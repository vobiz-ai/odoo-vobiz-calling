import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools import vobiz


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    vobiz_auth_id = fields.Char(related="company_id.vobiz_auth_id", readonly=False)
    # Write-only. What is typed here is encrypted onto the company, and the field
    # is cleared on save, so the token never reads back into a browser and does
    # not linger in this transient table.
    vobiz_auth_token = fields.Char("Vobiz Auth Token", groups="base.group_system")
    vobiz_auth_token_saved = fields.Boolean(compute="_compute_vobiz_auth_token_saved")
    vobiz_data_consent = fields.Boolean(related="company_id.vobiz_data_consent", readonly=False)
    # Picked from the account's numbers, so a number that is not on the account
    # (an agent's own mobile, say) cannot be set as the caller ID.
    vobiz_caller_id = fields.Selection(selection="_vobiz_caller_id_selection", string="Default caller ID")
    vobiz_app_id = fields.Char(related="company_id.vobiz_app_id")
    vobiz_public_url = fields.Char("Public URL for Vobiz", config_parameter="vobiz_calling.public_url")
    vobiz_ring_seconds = fields.Integer(
        "Ring time (seconds)", config_parameter="vobiz_calling.ring_seconds", default=25)
    vobiz_status = fields.Char(compute="_compute_vobiz_status")
    vobiz_status_kind = fields.Char(compute="_compute_vobiz_status")
    # Shown by the form itself, so the time is in the browser's time zone like
    # every other date on screen, not the user preference's.
    vobiz_checked_on = fields.Datetime(related="company_id.vobiz_checked_on")

    @api.model
    def _vobiz_caller_id_selection(self):
        return [(n, n) for n in self.env.company.sudo()._vobiz_numbers()]

    @api.depends("company_id")
    def _compute_vobiz_auth_token_saved(self):
        for settings in self:
            settings.vobiz_auth_token_saved = bool(settings.company_id.sudo().vobiz_auth_token_enc)

    @api.depends("company_id")
    def _compute_vobiz_status(self):
        # Says what the last check found, and when: nothing here is live, and an
        # address that stops working afterwards only shows at the next check.
        url = self.env["vobiz.session"]._public_url()
        for settings in self:
            company = settings.company_id.sudo()
            if not company._vobiz_configured():
                text, kind = _("Not connected yet: enter the Auth ID and Auth Token, then click "
                               "Save and check connection."), "muted"
            elif (not company.vobiz_checked_on or company.vobiz_checked_url != url
                    or not company.vobiz_app_id or company.vobiz_app_url != url):
                text, kind = _("Not checked for this address yet: click Save and check connection."), "warning"
            elif not company.vobiz_reach_ok:
                text, kind = _(
                    "At the last check, this Odoo did not answer at %s, so Vobiz cannot reach it and calls "
                    "will fail. Fix the address, or the proxy or tunnel in front of Odoo, then click Save "
                    "and check connection.", url), "warning"
            else:
                text, kind = _("Connected: %(count)s numbers. At the last check, this Odoo answered at %(url)s.",
                               count=len(company._vobiz_numbers()), url=url), "success"
            settings.vobiz_status = text
            settings.vobiz_status_kind = kind

    @api.model
    def get_values(self):
        res = super().get_values()
        company = self.env.company.sudo()
        number = company.vobiz_default_number
        res["vobiz_caller_id"] = number if number in company._vobiz_numbers() else False
        return res

    def set_values(self):
        company = self.company_id.sudo()
        sessions = self.env["vobiz.session"].sudo()
        auth_id_before = company.vobiz_auth_id
        url_before = sessions._public_url()
        super().set_values()
        token = (self.vobiz_auth_token or "").strip()
        if token:
            company._vobiz_set_auth_token(token)
        if token or company.vobiz_auth_id != auth_id_before:
            # Another account, or another token: find or make the application again.
            company.write({"vobiz_app_id": False, "vobiz_app_url": False})
        if company.vobiz_auth_id != auth_id_before:
            company.vobiz_numbers = "[]"
            sessions.search([("company_id", "=", company.id), ("mode", "=", "company"), ("active", "=", True)])._end(
                notify=_("Signed out: the company's Vobiz account changed. Sign in again."))
        if self.vobiz_caller_id:
            company.vobiz_default_number = self.vobiz_caller_id
        if sessions._public_url() != url_before:
            sessions._address_changed()
        if self.vobiz_auth_token:
            self.vobiz_auth_token = False

    def _vobiz_notify(self, message, kind="success"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Vobiz"),
                "message": message,
                "type": kind,
                "sticky": kind != "success",
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def action_vobiz_check(self):
        """Save, then check the credentials and set this Odoo up in the account."""
        self.ensure_one()
        self.execute()
        company = self.company_id.sudo()
        sessions = self.env["vobiz.session"]
        company._vobiz_require_consent()
        params = self.env["ir.config_parameter"].sudo()
        if not params.get_param("vobiz_calling.public_url"):
            # Pin the address now: the web base URL follows wherever an
            # administrator last logged in from, and calls must not follow it.
            base = (params.get_param("web.base.url") or "").strip().rstrip("/")
            if base.startswith("https://"):
                params.set_param("vobiz_calling.public_url", base)
        url = sessions._require_public_url()
        try:
            client = company._vobiz_client()
            numbers = client.list_numbers()
            app_id = company._vobiz_application(client, resync=True)
        except vobiz.VobizError as exc:
            raise UserError(sessions._error_text(exc)) from None
        e164s = [n["e164"] for n in numbers]
        reached, why = sessions._public_url_reaches_us()
        company.write({
            "vobiz_numbers": json.dumps(e164s),
            "vobiz_checked_on": fields.Datetime.now(),
            "vobiz_checked_url": url,
            "vobiz_reach_ok": reached,
        })
        if not numbers:
            return self._vobiz_notify(_(
                "Connected, but this Vobiz account has no active voice numbers to call from."), "warning")

        lines = [_("Connected to %(auth_id)s: %(count)s numbers, application %(app)s.",
                   auth_id=company.vobiz_auth_id, count=len(numbers), app=app_id)]
        kind = "success"
        if not company.vobiz_default_number:
            company.vobiz_default_number = e164s[0]
            lines.append(_("Default caller ID set to %s.", e164s[0]))
        elif company.vobiz_default_number not in e164s:
            lines.append(_("The default caller ID, %s, is not on this account: pick another.",
                           company.vobiz_default_number))
            kind = "warning"
        if reached:
            lines.append(_("Vobiz will reach this Odoo at %s.", url))
        else:
            lines.append(_(
                "But this Odoo did not answer at %(url)s (%(why)s), and Vobiz must reach it there or calls "
                "will fail. Check the Public URL, and your proxy or tunnel.", url=url, why=why))
            kind = "warning"
        return self._vobiz_notify(" ".join(lines), kind)

    def action_vobiz_enable_inbound(self):
        """Point the default number's incoming calls at this Odoo. An explicit action:
        it moves the number off whatever application it is on now."""
        self.ensure_one()
        self.execute()
        company = self.company_id.sudo()
        company._vobiz_require_consent()
        number = vobiz.to_e164(company.vobiz_default_number)
        if not number:
            raise UserError(_("Choose a number in Default caller ID first."))
        try:
            client = company._vobiz_client()
            by_number = {n["e164"]: n for n in client.list_numbers()}
            company.vobiz_numbers = json.dumps(list(by_number))
            if number not in by_number:
                raise UserError(_("%s is not an active voice number on this Vobiz account.", number))
            app_id = company._vobiz_application(client)
            before = by_number[number]["application_id"]
            if before == app_id:
                return self._vobiz_notify(_("Calls to %s already ring Odoo.", number))
            client.attach_number(number, app_id)
        except vobiz.VobizError as exc:
            raise UserError(self.env["vobiz.session"]._error_text(exc)) from None
        if before:
            return self._vobiz_notify(_(
                "Calls to %(number)s now ring Odoo. The number was on application %(before)s before.",
                number=number, before=before))
        return self._vobiz_notify(_("Calls to %s now ring Odoo.", number))
