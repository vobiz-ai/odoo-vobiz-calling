import logging
import re
import secrets

from odoo import http
from odoo.http import request

from ..tools import vobiz

_logger = logging.getLogger(__name__)

WEBHOOK = {"type": "http", "auth": "public", "methods": ["POST"], "csrf": False, "save_session": False}
CALL_REF_RE = re.compile(r"^[a-f0-9]{24}$")
PING_NONCE_RE = re.compile(r"^[a-f0-9]{32}$")
ROOM_RE = re.compile(r"^odoo[a-f0-9]{24}$")
HANGUP_OUTCOME = {
    "NO_ANSWER": "no_answer",
    "ORIGINATOR_CANCEL": "canceled",
    "USER_BUSY": "busy",
    "CALL_REJECTED": "busy",
}


class VobizController(http.Controller):

    # -- the softphone (signed-in Odoo users) -----------------------------

    @http.route("/vobiz/panel/state", type="json", auth="user")
    def panel_state(self):
        return request.env["vobiz.session"]._current()._panel_state()

    @http.route("/vobiz/panel/sign_in", type="json", auth="user")
    def panel_sign_in(self, mode, auth_id=None, auth_token=None, sip_username=None):
        """Sign in with an access token (mode company or own) or a SIP endpoint
        (mode sip). For an endpoint only its username comes here: its password
        stays in the browser, which registers with it."""
        session = request.env["vobiz.session"]._sign_in(
            mode, auth_id=auth_id, auth_token=auth_token, sip_username=sip_username)
        return session._panel_state()

    @http.route("/vobiz/panel/sign_out", type="json", auth="user")
    def panel_sign_out(self):
        sessions = request.env["vobiz.session"]
        sessions._current()._end()
        return sessions.browse()._panel_state()

    @http.route("/vobiz/panel/token", type="json", auth="user")
    def panel_token(self, force=False):
        """A fresh access token for the signed-in browser, or signedIn: false when
        the sign-in has ended (signed out, replaced by another tab, or expired)."""
        session = request.env["vobiz.session"]._current()
        if not session:
            return {"signedIn": False}
        if session.mode == "sip":
            return {"signedIn": True, "identity": session.identity, "mode": "sip"}
        token, exp = session._access_token(force=bool(force))
        return {"signedIn": True, "identity": session.identity, "accessToken": token, "accessTokenExpiresAt": exp}

    @http.route("/vobiz/panel/caller_id", type="json", auth="user")
    def panel_caller_id(self, number):
        session = request.env["vobiz.session"]._current()
        if session:
            session._set_caller_id(number)
        return session._panel_state(with_token=False)

    @http.route("/vobiz/panel/check_reach", type="json", auth="user")
    def panel_check_reach(self):
        """Asked by the panel after Vobiz refused a call before it started: does
        this Odoo answer at the address Vobiz calls? For signed-in agents only."""
        Session = request.env["vobiz.session"]
        if not Session._current():
            return {}
        Session = Session.sudo()
        reached, why = Session._public_url_reaches_us()
        return {"reached": reached, "url": Session._public_url(), "why": why}

    # -- the Public URL self-check ----------------------------------------------

    @http.route("/vobiz/ping", type="http", auth="public", methods=["GET"], csrf=False, save_session=False)
    def ping(self, nonce=None, **_kw):
        """Answers Settings' connection check, which calls this Odoo at its Public
        URL: only this database can answer the nonce. The answer is an HMAC in a
        scope of its own, so it is good for nothing else."""
        if not PING_NONCE_RE.match(nonce or ""):
            return request.make_response("", status=400)
        answer = request.env["vobiz.session"].sudo()._ping_answer(nonce)
        return request.make_response(answer, headers=[
            ("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store")])

    # -- Vobiz webhooks ------------------------------------------------------

    def _params(self):
        return {k: v for k, v in request.httprequest.form.items()}

    def _xml(self, inner=""):
        return request.make_response(
            vobiz.xml_doc(inner), headers=[("Content-Type", "application/xml; charset=utf-8")])

    def _forbidden(self):
        return request.make_response(vobiz.xml_doc(), status=403, headers=[("Content-Type", "application/xml")])

    def _verified(self, path, auth_id):
        """Whether Vobiz signed this request with the Auth Token of ``auth_id``.

        Every webhook is checked: an unsigned one is refused, and so is one for
        an account this Odoo holds no token for, since nothing could vouch for it.
        """
        tokens = request.env["vobiz.session"]._tokens_for(auth_id) if auth_id else []
        url = request.env["vobiz.session"]._webhook_url(path)
        valid = vobiz.signature_valid(request.httprequest.headers, url, tokens)
        if valid:
            return True
        why = "no signature" if valid is None else ("signature mismatch" if tokens else "no account to check it against")
        _logger.warning("Vobiz webhook %s rejected: %s", path, why)
        return False

    def _notify(self, user, payload):
        if user:
            user.sudo()._bus_send("vobiz/event", payload)

    @http.route("/vobiz/answer", **WEBHOOK)
    def answer(self, **_kw):
        p = self._params()
        if p.get("Event") == "Hangup":
            return self._hangup(p, "/vobiz/answer")
        if re.match(r"^sips?:", p.get("From", ""), re.I) or p.get("RouteType", "").lower() == "sip":
            return self._answer_outbound(p)
        return self._answer_inbound(p)

    def _answer_outbound(self, p):
        """The browser dialled out. Put its leg in a private room and dial the
        customer into it from the agent's caller ID."""
        Session = request.env["vobiz.session"]
        identity = vobiz.sip_user(p.get("From"))
        session = Session._for_identity(identity)
        if not self._verified("/vobiz/answer", session.auth_id if session else p.get("ParentAuthID")):
            return self._forbidden()
        if not session:
            _logger.info("Vobiz outbound from %s, which no signed-in agent holds: hung up", identity)
            return self._xml("<Hangup/>")
        destination = vobiz.to_e164(p.get("To"))
        if not destination:
            return self._xml("<Speak>That number cannot be dialled.</Speak><Hangup/>")
        numbers = session._numbers_list()
        asked = vobiz.to_e164(p.get("X-VH-Caller-ID") or p.get("SIPHeader_X-VH-Caller-ID"))
        caller_id = asked if asked in numbers else session.caller_id
        if caller_id not in numbers:
            return self._xml("<Speak>This account has no phone number to call from.</Speak><Hangup/>")

        Call = request.env["vobiz.call"].sudo()
        call = Call.create({
            "direction": "outbound",
            "partner_number": destination,
            "our_number": caller_id,
            "partner_id": Call._match_partner(destination, session.company_id).id,
            "user_id": session.user_id.id,
            "company_id": session.company_id.id,
            "call_uuid": p.get("CallUUID") or False,
            "auth_id": session.auth_id,
        })
        ref = p.get("X-VH-Call-Ref") or p.get("SIPHeader_X-VH-Call-Ref") or ""
        room = request.env["vobiz.room"].sudo().create({
            "name": f"odoo{secrets.token_hex(12)}",
            "session_id": session.id,
            "call_id": call.id,
            "auth_id": session.auth_id,
            "agent_call_uuid": p.get("CallUUID") or False,
            "call_ref": ref if CALL_REF_RE.match(ref) else False,
        })
        query = f"?room={room.name}"
        try:
            customer_leg = session._client().place_call(
                caller_id, destination,
                Session._webhook_url("/vobiz/outbound-join") + query,
                Session._webhook_url("/vobiz/outbound-hangup") + query)
        except vobiz.VobizError as exc:
            _logger.warning("Vobiz refused the customer leg of %s: %s", room.name, exc)
            call._finish("failed")
            return self._xml("<Speak>The call could not be placed.</Speak><Hangup/>")
        room.customer_call_uuid = customer_leg or False
        wait = vobiz.xml_escape(Session._webhook_url("/vobiz/outbound-wait"))
        events = vobiz.xml_escape(Session._webhook_url("/vobiz/conf-event") + query)
        return self._xml(
            f'<Conference stayAlone="true" startConferenceOnEnter="false" endConferenceOnExit="true" '
            f'waitSound="{wait}" timeLimit="14400" callbackUrl="{events}" callbackMethod="POST">'
            f"{room.name}</Conference><Hangup/>")

    def _answer_inbound(self, p):
        """A caller dialled one of the account's numbers. Ring every agent signed in
        to that account; the caller stays unanswered, and unbilled, until one accepts."""
        Session = request.env["vobiz.session"]
        did = vobiz.to_e164(p.get("To"))
        auth_id, company = p.get("ParentAuthID"), request.env["res.company"]
        if not auth_id:
            auth_id, company = Session._account_for_number(did)
        if not self._verified("/vobiz/answer", auth_id):
            return self._forbidden()
        sessions = Session._ringing_sessions(auth_id)
        if not company:
            company = sessions[:1].company_id or request.env["res.company"].sudo().search(
                [("vobiz_auth_id", "=", auth_id)], limit=1)
        caller = vobiz.to_e164(p.get("From"))
        Call = request.env["vobiz.call"].sudo()
        partner = Call._match_partner(caller, company or None)
        call = Call.create({
            "direction": "inbound",
            "partner_number": caller or p.get("From") or False,
            "our_number": did or False,
            "partner_id": partner.id,
            "company_id": (company or request.env.company).id,
            "call_uuid": p.get("CallUUID") or False,
            "auth_id": auth_id,
        })
        if not sessions:
            # Nobody to ring. A busy rejection leaves the caller unanswered, so
            # nothing is billed, rather than answering them to say so.
            call._finish("missed")
            return self._xml('<Hangup reason="busy"/>')
        users = "".join(
            f"<User>sip:{vobiz.xml_escape(s.identity)}@{vobiz.SIP_DOMAIN}</User>" for s in sessions)
        attrs = (
            f'callerId="{vobiz.xml_escape(did)}" timeout="{Session._ring_seconds()}" timeLimit="14400" '
            f'action="{vobiz.xml_escape(Session._webhook_url("/vobiz/dial-status"))}" method="POST" redirect="true" '
            f'callbackUrl="{vobiz.xml_escape(Session._webhook_url("/vobiz/dial-event"))}" callbackMethod="POST"')
        if caller:
            attrs += f' callerName="{vobiz.xml_escape(caller)}"'
        for session in sessions:
            self._notify(session.user_id, {
                "type": "incoming",
                "callUuid": call.call_uuid or "",
                "from": caller,
                "partnerId": partner.id or False,
                "partnerName": partner.name or "",
            })
        return self._xml(f"<Dial {attrs}>{users}</Dial>")

    @http.route("/vobiz/dial-status", **WEBHOOK)
    def dial_status(self, **_kw):
        """Where the inbound <Dial> hands the call when it ends. A caller nobody
        answered is turned away as busy, still unanswered; a taken call ends."""
        p = self._params()
        call = self._call_of(p)
        if not self._verified("/vobiz/dial-status", self._auth_id_of(call, p)):
            return self._forbidden()
        status = (p.get("DialStatus") or "").lower()
        call_status = (p.get("CallStatus") or "").lower()
        unanswered = call_status == "ringing"
        if call and unanswered:
            # A panel's Decline (486) arrives as DialStatus=completed with the
            # caller still ringing, as seen live; a ring that ran out as no-answer.
            outcome = "no_answer" if status in ("no-answer", "timeout") else (
                "declined" if status in ("busy", "completed") else "missed")
            call._finish(outcome)
        elif call and "cancel" in (status, call_status):
            call._finish("canceled")
        return self._xml('<Hangup reason="busy"/>' if unanswered else "<Hangup/>")

    @http.route("/vobiz/dial-event", **WEBHOOK)
    def dial_event(self, **_kw):
        """The inbound <Dial>'s callbackUrl. DialAnswer says which agent took the call."""
        p = self._params()
        call = self._call_of(p)
        if not self._verified("/vobiz/dial-event", self._auth_id_of(call, p)):
            return self._forbidden()
        answered = p.get("Event") == "DialAnswer" or (p.get("DialAction") or "").lower() == "answer"
        if call and answered and call.state != "done":
            Session = request.env["vobiz.session"]
            winner = Session._for_identity(vobiz.sip_user(p.get("DialBLegTo")))
            call._answered(user=winner.user_id or None)
            for session in Session._ringing_sessions(call.auth_id):
                if session != winner:
                    self._notify(session.user_id, {"type": "answeredElsewhere", "callUuid": call.call_uuid or ""})
        return self._xml()

    @http.route("/vobiz/hangup", **WEBHOOK)
    def hangup(self, **_kw):
        return self._hangup(self._params(), "/vobiz/hangup")

    def _hangup(self, p, path):
        """A leg of this application ended: the caller's on an inbound call, the
        agent's browser leg on an outbound one. Settle the call's log."""
        call = self._call_of(p)
        room = request.env["vobiz.room"].sudo().search(
            [("agent_call_uuid", "=", p.get("CallUUID") or "")], limit=1) if p.get("CallUUID") else None
        if room and not call:
            call = room.call_id
        if not self._verified(path, self._auth_id_of(call, p)):
            return self._forbidden()
        if room:
            room.agent_gone = True
            if not room.joined:
                # The agent hung up before the customer answered. Their phone
                # would ring on until it timed out, so cancel their leg.
                if call and call.state != "done":
                    call._finish("canceled")
                self._cancel_customer_leg(room)
        if call and call.state != "done":
            if call.answer_date:
                call._finish("answered")
            else:
                call._finish(HANGUP_OUTCOME.get((p.get("HangupCause") or "").upper(), "missed"))
        return self._xml()

    def _cancel_customer_leg(self, room):
        """Stop the customer's phone ringing: the agent left before they answered."""
        if room.customer_gone or not room.customer_call_uuid or not room.session_id:
            return
        try:
            room.session_id._client().hangup_call(room.customer_call_uuid)
            _logger.info("Vobiz outbound %s: the agent hung up first, so the customer's leg was cancelled",
                         room.name)
        except vobiz.VobizError as exc:
            _logger.info("Could not cancel the customer leg of %s: %s", room.name, exc)

    @http.route("/vobiz/outbound-join", **WEBHOOK)
    def outbound_join(self, room=None, **_kw):
        """The customer answered: bring them into the agent's room."""
        p = self._params()
        found = request.env["vobiz.room"]._by_name(room) if ROOM_RE.match(room or "") else None
        if not self._verified("/vobiz/outbound-join", found.auth_id if found else p.get("ParentAuthID")):
            return self._forbidden()
        if not found or p.get("Event") == "Hangup":
            return self._xml("<Hangup/>" if not found else "")
        if found.agent_gone:
            # The customer answered just as the agent hung up: nobody is there.
            return self._xml("<Hangup/>")
        found.joined = True
        found.call_id._answered()
        if found.session_id:
            self._notify(found.session_id.user_id, {"type": "outbound", "callRef": found.call_ref or "", "state": "connected"})
        return self._xml(
            '<Conference startConferenceOnEnter="true" stayAlone="false" endConferenceOnExit="true" '
            f'timeLimit="14400">{found.name}</Conference><Hangup/>')

    @http.route("/vobiz/outbound-hangup", **WEBHOOK)
    def outbound_hangup(self, room=None, **_kw):
        """The customer's leg ended. If they never answered, release the agent,
        who is otherwise left alone in a room that stays open."""
        p = self._params()
        found = request.env["vobiz.room"]._by_name(room) if ROOM_RE.match(room or "") else None
        if not self._verified("/vobiz/outbound-hangup", found.auth_id if found else p.get("ParentAuthID")):
            return self._forbidden()
        if not found:
            return self._xml()
        call = found.call_id
        found.customer_gone = True
        if found.joined:
            call._finish("answered")
        else:
            outcome = HANGUP_OUTCOME.get((p.get("HangupCause") or "").upper(), "failed")
            call._finish(outcome)
            if found.session_id and found.agent_call_uuid and not found.agent_gone:
                try:
                    found.session_id._client().hangup_call(found.agent_call_uuid)
                except vobiz.VobizError as exc:
                    _logger.info("Could not release the agent leg of %s: %s", found.name, exc)
        if found.session_id and not found.agent_gone:
            self._notify(found.session_id.user_id, {
                "type": "outbound", "callRef": found.call_ref or "", "state": "ended", "outcome": call.outcome or ""})
        return self._xml()

    @http.route("/vobiz/outbound-wait", **WEBHOOK)
    def outbound_wait(self, **_kw):
        # What the agent hears while the customer's phone rings. No side effects,
        # and nothing from the request is used.
        return self._xml('<Wait length="10"/>')

    @http.route("/vobiz/conf-event", **WEBHOOK)
    def conf_event(self, room=None, **_kw):
        found = request.env["vobiz.room"]._by_name(room) if ROOM_RE.match(room or "") else None
        if not self._verified("/vobiz/conf-event", found.auth_id if found else self._params().get("ParentAuthID")):
            return self._forbidden()
        return self._xml()

    # -- helpers -----------------------------------------------------------

    def _call_of(self, p):
        uuid = p.get("DialALegUUID") or p.get("CallUUID") or ""
        if not uuid:
            return request.env["vobiz.call"].sudo().browse()
        return request.env["vobiz.call"].sudo().search([("call_uuid", "=", uuid)], limit=1)

    def _auth_id_of(self, call, p):
        return (call.sudo().auth_id if call else None) or p.get("ParentAuthID")
