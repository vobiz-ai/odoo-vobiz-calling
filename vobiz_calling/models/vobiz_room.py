from datetime import timedelta

from odoo import api, fields, models


class VobizRoom(models.Model):
    """The private conference an outbound call runs through.

    The agent's browser leg waits in the room and the customer is dialled into
    it over the REST API. A <Dial><Number> straight from a WebRTC leg was
    measured losing the phone side's audio on three calls in five; a REST leg
    joined to a conference was clean every time.

    The room's name is random and is the only thing the customer leg's
    callbacks carry: the query string of a webhook is not signed, so nothing
    else is taken from it.
    """

    _name = "vobiz.room"
    _description = "Vobiz outbound call room"

    name = fields.Char(required=True, index=True)
    session_id = fields.Many2one("vobiz.session", ondelete="set null")
    call_id = fields.Many2one("vobiz.call", ondelete="cascade")
    auth_id = fields.Char()
    agent_call_uuid = fields.Char()
    # The customer's leg, as Vobiz numbered it when it was placed (its
    # request_uuid, which Vobiz documents as the call's uuid). An agent who
    # hangs up before the customer answers cancels it by this.
    customer_call_uuid = fields.Char()
    call_ref = fields.Char()
    joined = fields.Boolean()
    # Which legs have ended, as their hang-up webhooks arrive.
    agent_gone = fields.Boolean()
    customer_gone = fields.Boolean()

    _sql_constraints = [("name_unique", "unique(name)", "Room names are unique.")]

    @api.model
    def _by_name(self, name):
        return self.sudo().search([("name", "=", name or "")], limit=1) if name else self.sudo().browse()

    @api.model
    def _cron_clean(self):
        self.sudo().search([("create_date", "<", fields.Datetime.now() - timedelta(days=1))]).unlink()
