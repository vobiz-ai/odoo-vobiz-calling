import hashlib
import json
import logging
import secrets
import time
from datetime import timedelta
from urllib.parse import urlsplit

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import hmac as odoo_hmac

from ..tools import vobiz

_logger = logging.getLogger(__name__)

SESSION_HOURS = 12
# A cached access token is handed out again while it has this long left.
TOKEN_REUSE_MIN_SECONDS = 120
# The HMAC scope of the Public URL self-check, so its answers are good for nothing else.
PING_SCOPE = "vobiz_calling.ping"


class VobizSession(models.Model):
    """An agent signed in to the softphone.

    One active sign-in per user: signing in again (another tab, another account)
    ends the previous one, so a user's calls ring one browser. Two ways in:

    * an access token (``company`` or ``own`` mode): the browser registers as
      ``<sub>_<auth_id>`` with a short-lived JWT Odoo has Vobiz mint, and never
      receives an Auth Token;
    * a SIP endpoint (``sip`` mode): the browser registers as the endpoint, with
      the password the agent typed there. Odoo never receives that password; it
      only checks that an administrator allowed the endpoint.

    Only reached through sudo() by this module's controllers: no group has
    access rights on this model.
    """

    _name = "vobiz.session"
    _description = "Vobiz softphone sign-in"
    _order = "id desc"

    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", required=True, ondelete="cascade")
    mode = fields.Selection(
        [("company", "Company account"), ("own", "Own Vobiz account"), ("sip", "SIP endpoint")], required=True)
    auth_id = fields.Char(required=True, index=True)
    # Only for an agent's own account. The company's token lives on the company.
    auth_token_enc = fields.Char()
    app_id = fields.Char(required=True)
    sub = fields.Char(required=True)
    identity = fields.Char(required=True, index=True)
    numbers = fields.Text(default="[]")
    caller_id = fields.Char()
    access_token = fields.Char()
    access_token_exp = fields.Integer()
    expires_at = fields.Datetime(required=True)
    active = fields.Boolean(default=True, index=True)

    _sql_constraints = [
        ("identity_unique", "unique(identity)", "Each sign-in has an identity of its own."),
    ]

    # -- where Vobiz reaches this Odoo ------------------------------------

    @api.model
    def _public_url(self):
        params = self.env["ir.config_parameter"].sudo()
        url = params.get_param("vobiz_calling.public_url") or params.get_param("web.base.url") or ""
        return url.strip().rstrip("/")

    @api.model
    def _webhook_url(self, path):
        return f"{self._public_url()}{path}"

    @api.model
    def _ring_seconds(self):
        raw = self.env["ir.config_parameter"].sudo().get_param("vobiz_calling.ring_seconds") or 25
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            seconds = 25
        return min(max(seconds, 5), 120)

    @api.model
    def _app_name(self):
        """This Odoo's application name in Vobiz, naming the address it calls."""
        host = urlsplit(self._public_url()).hostname or "odoo"
        return f"Odoo-Calling ({host})"[:80]

    @api.model
    def _ensure_application(self, client):
        return client.ensure_application(
            self._webhook_url("/vobiz/answer"),
            self._webhook_url("/vobiz/hangup"),
            self._app_name(),
        )

    @api.model
    def _require_public_url(self):
        """The https:// address Vobiz reaches this Odoo at, or a clear refusal."""
        url = self._public_url()
        if not url.startswith("https://"):
            raise UserError(_(
                "Vobiz must reach this Odoo over HTTPS, and the Public URL for Vobiz is %s. "
                "An administrator can set it in Settings, under Vobiz.", url or _("empty")))
        return url

    @api.model
    def _ping_answer(self, nonce):
        return odoo_hmac(self.env(su=True), PING_SCOPE, nonce)

    @api.model
    def _public_url_reaches_us(self, timeout=8):
        """Whether this very Odoo answers at its Public URL, the way Vobiz will
        reach it. Returns (reached, why_not)."""
        nonce = secrets.token_hex(16)
        try:
            response = requests.get(
                self._webhook_url("/vobiz/ping"), params={"nonce": nonce}, timeout=timeout, allow_redirects=False)
        except requests.RequestException as exc:
            return False, _("no answer: %s", type(exc).__name__)
        if response.status_code != 200:
            return False, _("it answered HTTP %s", response.status_code)
        if response.text.strip() != self._ping_answer(nonce):
            return False, _("another server answered")
        return True, ""

    @api.model
    def _error_text(self, error):
        """What to tell a person about a failed Vobiz call. Never a credential."""
        if error.status == 401:
            return _("Vobiz did not accept that Auth ID and Auth Token.")
        if error.status == 0:
            return _("Could not reach Vobiz. Check this server's internet access and try again.")
        reason = getattr(error, "reason", None)
        if reason and "url" in reason.lower():
            return _(
                "Vobiz cannot use this Odoo's address, %(url)s: %(reason)s. Check the Public URL for "
                "Vobiz in Settings, and that it opens this Odoo from the internet.",
                url=self._public_url() or _("empty"), reason=reason)
        if reason:
            return _("Vobiz refused the request: %s", reason)
        return str(error)

    @api.model
    def _address_changed(self):
        """The Public URL moved. Point each company's application at the new
        address, so its numbers keep ringing Odoo, and sign out the agents on
        their own accounts, whose applications were made for the old one."""
        for company in self.env["res.company"].sudo().search([("vobiz_app_id", "!=", False)]):
            try:
                company._vobiz_application()
            except (vobiz.VobizError, UserError) as exc:
                _logger.warning("Could not move the Vobiz application of company %s: %s", company.id, exc)
        self.sudo().search([("mode", "=", "own"), ("active", "=", True)])._end(
            notify=_("Signed out: this Odoo's address for Vobiz changed. Sign in again."))

    # -- signing in and out --------------------------------------------------

    def _numbers_list(self):
        self.ensure_one()
        try:
            numbers = json.loads(self.numbers or "[]")
        except ValueError:
            numbers = []
        return [n for n in numbers if isinstance(n, str)]

    def _auth_token(self):
        self.ensure_one()
        if self.mode in ("company", "sip"):
            # A SIP endpoint signs in on the company's account: its calls are
            # placed, and its webhooks checked, with the company's credentials.
            return self.company_id._vobiz_auth_token()
        return vobiz.decrypt(self.company_id._vobiz_secret(), self.auth_token_enc)

    def _client(self):
        self.ensure_one()
        return vobiz.VobizClient(self.auth_id, self._auth_token())

    def _live(self):
        self.ensure_one()
        return self.active and self.expires_at and self.expires_at > fields.Datetime.now()

    @api.model
    def _current(self):
        """The signed-in user's live sign-in, or an empty recordset."""
        session = self.sudo().search([("user_id", "=", self.env.uid), ("active", "=", True)], limit=1)
        return session if session and session._live() else self.sudo().browse()

    @api.model
    def _sip_endpoint_for(self, company, user, username):
        """The endpoint ``username`` if this user may sign in with it now, or a refusal."""
        username = (username or "").strip()
        if not vobiz.SIP_USER_RE.match(username):
            raise UserError(_("Enter the SIP endpoint's username, as shown in the Vobiz Console under Endpoints."))
        endpoint = self.env["vobiz.endpoint"].sudo().search(
            [("company_id", "=", company.id), ("username", "=", username), ("allowed", "=", True)], limit=1)
        if not endpoint:
            raise UserError(_(
                "%s is not a SIP endpoint this Odoo may use. An administrator can allow it under "
                "Vobiz > SIP endpoints.", username))
        if endpoint.user_id and endpoint.user_id != user:
            raise UserError(_("The SIP endpoint %s is kept for another Odoo user.", username))
        holder = self._for_identity(username)
        if holder and holder.user_id != user:
            raise UserError(_(
                "%(name)s is signed in with the SIP endpoint %(endpoint)s. One person at a time can use an endpoint.",
                name=holder.user_id.name, endpoint=username))
        return endpoint

    @api.model
    def _sign_in(self, mode, auth_id=None, auth_token=None, sip_username=None):
        """Sign the current user in, ending any sign-in they already had."""
        user = self.env.user
        company = self.env.company.sudo()
        company._vobiz_require_consent()
        self._require_public_url()
        endpoint = None
        if mode in ("company", "sip"):
            client = company._vobiz_client()
            auth_id = company.vobiz_auth_id
            if mode == "sip":
                endpoint = self._sip_endpoint_for(company, user, sip_username)
        elif mode == "own":
            auth_id = (auth_id or "").strip()
            auth_token = (auth_token or "").strip()
            if not auth_id or not auth_token:
                raise UserError(_("Enter both the Auth ID and the Auth Token."))
            try:
                client = vobiz.VobizClient(auth_id, auth_token)
            except vobiz.VobizError as exc:
                raise UserError(self._error_text(exc)) from None
        else:
            raise UserError(_("Unknown sign-in mode."))

        try:
            numbers = [n["e164"] for n in client.list_numbers()]
            app_id = self._ensure_application(client) if mode == "own" else company._vobiz_application(client)
            # Calls placed from an endpoint go to the application linked to it
            # now, which may have changed in the Vobiz Console since it was allowed.
            linked_now = client.endpoint_app_id(endpoint.endpoint_id) if endpoint else None
        except vobiz.VobizError as exc:
            _logger.info("Vobiz sign-in refused for user %s on %s: %s", user.id, auth_id, exc)
            raise UserError(self._error_text(exc)) from None
        if endpoint and linked_now != app_id:
            raise UserError(_(
                "Calls from the SIP endpoint %s don't go to Odoo's Vobiz application any more. An administrator "
                "can click Use with Odoo for it, under Vobiz > SIP endpoints.", endpoint.username))
        if mode in ("company", "sip"):
            company.vobiz_numbers = json.dumps(numbers)

        previous = self.sudo().search([("user_id", "=", user.id), ("active", "=", True)])
        caller_id = next((n for n in (previous[:1].caller_id, company.vobiz_default_number) if n in numbers), None)
        previous._end()

        if endpoint:
            # The browser registers as the endpoint itself.
            sub = identity = endpoint.username
        else:
            # The identity names this database and user, plus a fresh part per
            # sign-in, so an older browser's registration can never take the new
            # one's calls. Vobiz allows 50 characters for it.
            stem = hashlib.sha256(f"{self.env.cr.dbname}:{user.id}".encode()).hexdigest()[:12]
            sub = f"odoo-{stem}-{secrets.token_hex(3)}"
            identity = f"{sub}_{auth_id}"
        session = self.sudo().create({
            "user_id": user.id,
            "company_id": company.id,
            "mode": mode,
            "auth_id": auth_id,
            "auth_token_enc": vobiz.encrypt(company._vobiz_secret(), auth_token) if mode == "own" else False,
            "app_id": app_id,
            "sub": sub,
            "identity": identity,
            "numbers": json.dumps(numbers),
            "caller_id": caller_id or (numbers[0] if numbers else False),
            "expires_at": fields.Datetime.now() + timedelta(hours=SESSION_HOURS),
        })
        if mode != "sip":
            session._access_token(force=True)
        _logger.info("Vobiz sign-in: user %s on %s (%s), identity %s", user.id, auth_id, mode, session.identity)
        return session

    def _end(self, notify=None):
        """End these sign-ins. Their identities stop ringing and their tokens are
        forgotten. With ``notify``, each browser is told at once, with that text."""
        if not self:
            return
        if notify:
            for session in self.sudo():
                session.user_id.sudo()._bus_send("vobiz/event", {
                    "type": "signedOut", "identity": session.identity, "message": notify})
        # An endpoint's username is its identity on every sign-in with it: free
        # it for the next one, since identities are unique.
        for session in self.sudo().filtered(lambda s: s.mode == "sip" and s.active):
            session.identity = f"{session.identity}~{session.id}"
        self.sudo().write({"active": False, "access_token": False, "auth_token_enc": False})
        # Written now: the next sign-in with the same endpoint inserts its name at once.
        self.sudo().flush_recordset(["identity", "active"])

    def _access_token(self, force=False):
        """The access token the browser should present now, minted when needed."""
        self.ensure_one()
        if self.mode == "sip":
            raise UserError(_("A SIP endpoint signs in with its password, not an access token."))
        now = int(time.time())
        if not force and self.access_token and self.access_token_exp - now > TOKEN_REUSE_MIN_SECONDS:
            return self.access_token, self.access_token_exp
        try:
            token, exp = self._client().mint_access_token(self.sub, self.app_id)
        except vobiz.VobizError as exc:
            raise UserError(self._error_text(exc)) from None
        self.sudo().write({"access_token": token, "access_token_exp": exp})
        return token, exp

    def _set_caller_id(self, number):
        self.ensure_one()
        number = vobiz.to_e164(number)
        if number not in self._numbers_list():
            raise UserError(_("%s is not a number on this Vobiz account.", number or _("That")))
        self.sudo().caller_id = number

    def _panel_state(self, with_token=True):
        """What the softphone needs. The Auth Token is never part of it."""
        company = self.env.company.sudo()
        state = {
            "signedIn": False,
            "companyAccount": company._vobiz_configured(),
            "registrar": vobiz.REGISTRAR_URL,
            "sipDomain": vobiz.SIP_DOMAIN,
        }
        if self:
            self.ensure_one()
            state.update({
                "signedIn": True,
                "mode": self.mode,
                "authId": self.auth_id,
                "identity": self.identity,
                "numbers": self._numbers_list(),
                "callerId": self.caller_id or "",
            })
            if with_token and self.mode != "sip":
                token, exp = self._access_token()
                state.update({"accessToken": token, "accessTokenExpiresAt": exp})
        return state

    # -- webhooks ----------------------------------------------------------

    @api.model
    def _for_identity(self, identity):
        session = self.sudo().search([("identity", "=", identity or ""), ("active", "=", True)], limit=1)
        return session if session and session._live() else self.sudo().browse()

    @api.model
    def _tokens_for(self, auth_id):
        """Every Auth Token this Odoo holds for an account: the company's, and
        those of agents signed in with it as their own."""
        tokens = []
        companies = self.env["res.company"].sudo().search([("vobiz_auth_id", "=", auth_id or "")])
        tokens.extend(c._vobiz_auth_token() for c in companies)
        own = self.sudo().search([("auth_id", "=", auth_id or ""), ("mode", "=", "own"), ("active", "=", True)])
        tokens.extend(s._auth_token() for s in own if s._live())
        return [t for t in dict.fromkeys(tokens) if t]

    @api.model
    def _account_for_number(self, e164):
        """The account (auth_id, company) whose number this is, by the numbers
        seen at sign-in and the companies' default numbers."""
        if not e164:
            return None, self.env["res.company"]
        for session in self.sudo().search([("active", "=", True)]):
            if session._live() and e164 in session._numbers_list():
                return session.auth_id, session.company_id
        company = self.env["res.company"].sudo().search([("vobiz_default_number", "=", e164)], limit=1)
        if company and company.vobiz_auth_id:
            return company.vobiz_auth_id, company
        return None, self.env["res.company"]

    @api.model
    def _ringing_sessions(self, auth_id):
        """The live sign-ins an inbound call to this account should ring."""
        sessions = self.sudo().search([("auth_id", "=", auth_id or ""), ("active", "=", True)])
        return sessions.filtered(lambda s: s._live())

    @api.model
    def _cron_expire(self):
        expired = self.sudo().search([("active", "=", True), ("expires_at", "<=", fields.Datetime.now())])
        expired._end()
        old = self.sudo().with_context(active_test=False).search([
            ("active", "=", False), ("write_date", "<", fields.Datetime.now() - timedelta(days=7))])
        old.unlink()
