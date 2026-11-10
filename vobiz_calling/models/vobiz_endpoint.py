import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools import vobiz

_logger = logging.getLogger(__name__)


class VobizEndpoint(models.Model):
    """A SIP endpoint of the company's Vobiz account that agents may sign in with.

    Vobiz sends a call placed from an endpoint to the application linked to that
    endpoint, so an endpoint is usable in Odoo once it is linked to Odoo's
    application. Only administrators load, link and allow endpoints. The
    endpoint's password never reaches Odoo: the agent types it in the browser,
    which registers with it.
    """

    _name = "vobiz.endpoint"
    _description = "Vobiz SIP endpoint"
    _order = "username"

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="cascade", default=lambda self: self.env.company)
    endpoint_id = fields.Char("Endpoint ID", required=True, readonly=True)
    username = fields.Char("SIP username", required=True, readonly=True, index=True)
    alias = fields.Char(readonly=True)
    app_id = fields.Char(
        "Vobiz application", readonly=True,
        help="The Vobiz application this endpoint's calls go to, as of the last load or link.")
    linked = fields.Boolean("Calls go to Odoo", compute="_compute_linked")
    allowed = fields.Boolean("Used in Odoo", readonly=True)
    user_id = fields.Many2one(
        "res.users", "Agent", domain=[("share", "=", False)],
        help="Only this user can sign in with the endpoint. Leave empty to let any user of the company.")
    in_use_by = fields.Many2one("res.users", "Signed in now", compute="_compute_in_use_by")

    _sql_constraints = [
        ("username_company_unique", "unique(company_id, username)", "Each SIP username appears once per company."),
    ]

    @api.depends("app_id", "company_id")
    def _compute_linked(self):
        for endpoint in self:
            app_id = endpoint.company_id.sudo().vobiz_app_id
            endpoint.linked = bool(app_id) and endpoint.app_id == app_id

    def _compute_in_use_by(self):
        Session = self.env["vobiz.session"].sudo()
        for endpoint in self:
            session = Session._for_identity(endpoint.username)
            endpoint.in_use_by = session.user_id if session and session.mode == "sip" else False

    def _notify(self, message, kind="success"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Vobiz"),
                "message": message,
                "type": kind,
                "sticky": kind != "success",
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def _error(self, exc):
        return UserError(self.env["vobiz.session"]._error_text(exc))

    def action_load(self):
        """Read the company account's endpoints from Vobiz, and what each is linked to.

        A list-header button: the web client calls it on the selected rows,
        usually none, so it takes a recordset (empty or not) and ignores it."""
        company = self.env.company.sudo()
        try:
            found = company._vobiz_client().endpoints()
        except vobiz.VobizError as exc:
            raise self._error(exc) from None
        mine = self.sudo().search([("company_id", "=", company.id)])
        by_username = {e.username: e for e in mine}
        for item in found:
            values = {"endpoint_id": item["endpoint_id"], "alias": item["alias"], "app_id": item["app_id"] or False}
            if item["username"] in by_username:
                by_username[item["username"]].write(values)
            else:
                self.sudo().create({**values, "company_id": company.id, "username": item["username"]})
        usernames = {item["username"] for item in found}
        gone = mine.filtered(lambda e: e.username not in usernames)
        gone._stop_sessions(_("Signed out: this SIP endpoint is no longer on the company's Vobiz account."))
        gone.unlink()
        return self._notify(_("%s SIP endpoints loaded from Vobiz.", len(found)))

    def action_use_in_odoo(self):
        """Link these endpoints to Odoo's application, and let agents sign in with them."""
        for endpoint in self.sudo():
            company = endpoint.company_id
            company._vobiz_require_consent()
            try:
                client = company._vobiz_client()
                app_id = company._vobiz_application(client)
                if endpoint.app_id != app_id:
                    client.link_endpoint(endpoint.endpoint_id, app_id)
                    _logger.info("Vobiz endpoint %s linked to application %s (was %s)",
                                 endpoint.username, app_id, endpoint.app_id or "none")
            except vobiz.VobizError as exc:
                raise self._error(exc) from None
            endpoint.write({"app_id": app_id, "allowed": True})
        return self._notify(_("Calls from %s now go through Odoo, and agents can sign in with it.",
                              ", ".join(self.mapped("username"))))

    def action_stop_using(self):
        """Agents can no longer sign in with these endpoints. Vobiz keeps sending
        their calls to Odoo's application until they are linked elsewhere."""
        self.sudo().write({"allowed": False})
        self._stop_sessions(_("Signed out: an administrator stopped this SIP endpoint's use in Odoo."))
        return self._notify(_("Agents can no longer sign in with %s.", ", ".join(self.mapped("username"))))

    def _stop_sessions(self, message):
        Session = self.env["vobiz.session"].sudo()
        for endpoint in self:
            Session.search([("identity", "=", endpoint.username), ("mode", "=", "sip"), ("active", "=", True)])._end(
                notify=message)
