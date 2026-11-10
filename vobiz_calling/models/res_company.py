import json
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..tools import vobiz

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    _inherit = "res.company"

    # The company's Vobiz account, which every agent of the company can use.
    # The Auth Token is stored encrypted, and only administrators can read or
    # write any of these fields; the softphone reaches them through sudo().
    vobiz_auth_id = fields.Char("Vobiz Auth ID", groups="base.group_system", copy=False)
    vobiz_auth_token_enc = fields.Char("Vobiz Auth Token (encrypted)", groups="base.group_system", copy=False)
    vobiz_app_id = fields.Char("Vobiz application", groups="base.group_system", copy=False)
    # The address the application sends calls to. When this Odoo's Public URL
    # moves, the application is pointed at the new one.
    vobiz_app_url = fields.Char("Vobiz application address", groups="base.group_system", copy=False)
    # The account's voice numbers as of the last check or company sign-in.
    vobiz_numbers = fields.Text("Vobiz numbers", groups="base.group_system", copy=False, default="[]")
    vobiz_checked_on = fields.Datetime("Vobiz connection checked on", groups="base.group_system", copy=False)
    # What that check found: the address it called this Odoo at, and whether
    # this Odoo answered there, the way Vobiz has to reach it.
    vobiz_checked_url = fields.Char("Vobiz address checked", groups="base.group_system", copy=False)
    vobiz_reach_ok = fields.Boolean("Vobiz address answered", groups="base.group_system", copy=False)
    # The administrator's opt-in: phone numbers and call details go to Vobiz.
    vobiz_data_consent = fields.Boolean("Allow calls through Vobiz", groups="base.group_system", copy=False)
    vobiz_default_number = fields.Char("Default caller ID", copy=False)

    def _vobiz_secret(self):
        return self.env["ir.config_parameter"].sudo().get_param("database.secret")

    def _vobiz_set_auth_token(self, token):
        self.ensure_one()
        self.sudo().vobiz_auth_token_enc = vobiz.encrypt(self._vobiz_secret(), token) if token else False

    def _vobiz_auth_token(self):
        self.ensure_one()
        return vobiz.decrypt(self._vobiz_secret(), self.sudo().vobiz_auth_token_enc)

    def _vobiz_configured(self):
        self.ensure_one()
        company = self.sudo()
        return bool(company.vobiz_auth_id and company.vobiz_auth_token_enc)

    def _vobiz_numbers(self):
        """The account's voice numbers seen at the last check, in E.164."""
        self.ensure_one()
        try:
            numbers = json.loads(self.sudo().vobiz_numbers or "[]")
        except ValueError:
            numbers = []
        return [n for n in numbers if isinstance(n, str)]

    def _vobiz_require_consent(self):
        self.ensure_one()
        if not self.sudo().vobiz_data_consent:
            raise UserError(_(
                "Calls through Vobiz are not allowed yet for %s. An administrator can allow them "
                "in Settings, under Vobiz.", self.name))

    def _vobiz_client(self):
        self.ensure_one()
        if not self._vobiz_configured():
            raise UserError(_(
                "Vobiz is not set up for %s. An administrator can add the Auth ID and "
                "Auth Token in Settings.", self.name))
        return vobiz.VobizClient(self.sudo().vobiz_auth_id, self._vobiz_auth_token())

    def _vobiz_application(self, client=None, resync=False):
        """This Odoo's application in the company's account, pointing at the
        current Public URL: found or created once, and moved when the URL moves.
        With ``resync``, its addresses and name are written again even if the
        URL did not move, which repairs an application edited in the Console."""
        self.ensure_one()
        company = self.sudo()
        Session = self.env["vobiz.session"]
        url = Session._public_url()
        if company.vobiz_app_id and company.vobiz_app_url == url and not resync:
            return company.vobiz_app_id
        client = client or self._vobiz_client()
        if company.vobiz_app_id:
            # Same application, new address: the numbers already ringing Odoo,
            # and the agents already signed in, follow without anything else to do.
            try:
                client.update_application(
                    company.vobiz_app_id, Session._webhook_url("/vobiz/answer"), Session._webhook_url("/vobiz/hangup"),
                    app_name=Session._app_name())
                company.vobiz_app_url = url
                _logger.info("Vobiz application %s now points at %s", company.vobiz_app_id, url)
                return company.vobiz_app_id
            except vobiz.VobizError as exc:
                if exc.status != 404:
                    raise
                _logger.info("Vobiz application %s is gone; making another", company.vobiz_app_id)
        app_id = Session._ensure_application(client)
        company.write({"vobiz_app_id": app_id, "vobiz_app_url": url})
        return app_id
