import re

from markupsafe import Markup

from odoo import _, api, fields, models

from ..tools import vobiz

OUTCOMES = [
    ("answered", "Answered"),
    ("missed", "Missed"),
    ("declined", "Declined"),
    ("no_answer", "Not answered"),
    ("busy", "Busy"),
    ("failed", "Failed"),
    ("canceled", "Caller hung up"),
]


class VobizCall(models.Model):
    """One call, in or out. Written by the Vobiz webhooks, once per call: the
    Odoo server sees every event of a call, so no browser has to log it."""

    _name = "vobiz.call"
    _description = "Vobiz call"
    _order = "start_date desc, id desc"

    direction = fields.Selection([("inbound", "Incoming"), ("outbound", "Outgoing")], required=True)
    state = fields.Selection(
        [("ringing", "Ringing"), ("in_progress", "In progress"), ("done", "Done")],
        default="ringing", required=True)
    outcome = fields.Selection(OUTCOMES)
    partner_number = fields.Char("Number", help="The other party's number, in E.164.")
    our_number = fields.Char("Vobiz number")
    partner_id = fields.Many2one("res.partner", "Contact", index=True)
    user_id = fields.Many2one("res.users", "Agent", index=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    start_date = fields.Datetime("Started", required=True, default=fields.Datetime.now)
    answer_date = fields.Datetime("Answered")
    end_date = fields.Datetime("Ended")
    talk_seconds = fields.Integer("Talk time (s)")
    talk_time = fields.Char("Talk time", compute="_compute_talk_time")
    call_uuid = fields.Char("Vobiz call UUID", index=True, copy=False)
    auth_id = fields.Char("Vobiz account", groups="base.group_system")
    posted = fields.Boolean(copy=False)

    @api.depends("direction", "partner_number", "partner_id")
    def _compute_display_name(self):
        for call in self:
            who = call.partner_id.name or call.partner_number or _("unknown number")
            call.display_name = _("Call from %s", who) if call.direction == "inbound" else _("Call to %s", who)

    @api.depends("talk_seconds")
    def _compute_talk_time(self):
        for call in self:
            call.talk_time = _talk_time(call.talk_seconds) if call.talk_seconds else ""

    @api.model
    def _match_partner(self, e164, company=None):
        """The contact with this number on its phone or mobile, however it was typed."""
        if not e164:
            return self.env["res.partner"]
        partners = self.env["res.partner"].sudo().with_context(active_test=True)
        domain_company = [("company_id", "in", [False, company.id])] if company else []
        if "phone_sanitized" in partners._fields:
            found = partners.search([("phone_sanitized", "=", e164)] + domain_company, limit=1)
            if found:
                return found
        # Compare digits on the last few, then normalise the candidates here:
        # a stored "+91 98765 43210" or "098765 43210" is the same number.
        tail = re.sub(r"\D", "", e164)[-6:]
        if not tail:
            return self.env["res.partner"]
        like = "%".join(tail)
        candidates = partners.search(
            ["|", ("phone", "ilike", like), ("mobile", "ilike", like)] + domain_company, limit=50)
        for partner in candidates:
            if e164 in (vobiz.to_e164(partner.phone), vobiz.to_e164(partner.mobile)):
                return partner
        return self.env["res.partner"]

    def _answered(self, user=None, when=None):
        for call in self.sudo():
            if call.state == "done":
                continue
            values = {"state": "in_progress", "answer_date": call.answer_date or when or fields.Datetime.now()}
            if user:
                values["user_id"] = user.id
            call.write(values)

    def _finish(self, outcome=None, when=None):
        """The call is over: settle its outcome and talk time, and log it on the contact."""
        when = when or fields.Datetime.now()
        for call in self.sudo():
            if call.state == "done":
                continue
            final = outcome or ("answered" if call.answer_date else "missed")
            if final == "answered" and not call.answer_date:
                final = "missed"
            talk = int((when - call.answer_date).total_seconds()) if call.answer_date else 0
            call.write({"state": "done", "outcome": final, "end_date": when, "talk_seconds": max(talk, 0)})
            call._post_on_partner()

    def _post_on_partner(self):
        """Note the call on the contact's chatter, once, as the agent."""
        for call in self.sudo():
            if call.posted or not call.partner_id:
                continue
            outcome = dict(OUTCOMES).get(call.outcome, call.outcome or "")
            lines = [
                (_("Direction"), dict(self._fields["direction"].selection).get(call.direction)),
                (_("Outcome"), outcome),
                (_("Number"), call.partner_number),
                (_("Vobiz number"), call.our_number),
                (_("Agent"), call.user_id.name),
                (_("Talk time"), call.talk_time or _("none")),
            ]
            body = Markup("<p><b>%s</b></p><ul>%s</ul>") % (
                _("Vobiz call"),
                Markup("").join(
                    Markup("<li>%s: %s</li>") % (label, value) for label, value in lines if value),
            )
            call.partner_id.message_post(
                body=body,
                message_type="comment",
                subtype_xmlid="mail.mt_note",
                author_id=call.user_id.partner_id.id if call.user_id else None,
            )
            call.posted = True


def _talk_time(seconds):
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} min {rest} s" if minutes else f"{rest} s"
