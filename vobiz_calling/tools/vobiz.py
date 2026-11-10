"""Talking to Vobiz: its REST API, its webhook signatures, and the XML it runs.

Nothing in this file touches the database. Auth Tokens are passed in by the
caller and are never logged, returned in an error, or put in a URL.
"""
import base64
import hashlib
import hmac
import re
import time
from urllib.parse import quote

import requests
from cryptography.fernet import Fernet, InvalidToken

API_BASE = "https://api.vobiz.ai"
REGISTRAR_URL = "wss://registrar.vobiz.ai:5063/"
SIP_DOMAIN = "registrar.vobiz.ai"

# Vobiz accepts access tokens living 180 to 86400 seconds. A token cannot be
# revoked, so its lifetime is the longest a leaked one stays usable: keep it
# short and let the browser renew it a minute before it runs out.
JWT_TTL_SECONDS = 900

AUTH_ID_RE = re.compile(r"^[A-Za-z0-9_]{2,64}$")
SUB_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
JWT_SHAPE_RE = re.compile(r"^[A-Za-z0-9_\-+/=]+\.[A-Za-z0-9_\-+/=]+\.[A-Za-z0-9_\-+/=]+$")
DIGITS_RE = re.compile(r"^\d{1,20}$")
# A SIP endpoint's username, as the Vobiz Console makes them.
SIP_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Vobiz's list pages hold this many objects at most.
PAGE_SIZE = 20
# An endpoint's "application" is a resource URI: /v1/Account/<auth_id>/Application/<app_id>/.
APP_URI_RE = re.compile(r"/Application/(\d{1,20})/?$")


def app_id_of(application):
    """The application id in an endpoint's "application" field. Vobiz sends a
    resource URI or null (seen live); its OpenAPI schema also allows an object."""
    if isinstance(application, dict):
        value = str(application.get("app_id") or "")
    else:
        match = APP_URI_RE.search(str(application or ""))
        value = match.group(1) if match else ""
    return value if DIGITS_RE.match(value) else ""


class VobizError(Exception):
    """A Vobiz API call failed. The message names the status, never a credential.

    ``status`` is the HTTP status, or 0 when Vobiz could not be reached.
    ``reason`` is Vobiz's own short explanation, when it gave one.
    """

    def __init__(self, message, status=None, reason=None):
        super().__init__(message)
        self.status = status
        self.reason = reason


REASON_MAX = 200


def error_reason(body, secret=""):
    """Vobiz's explanation from an error body, short, on one line, and never
    containing ``secret`` (the Auth Token used for the request)."""
    if not isinstance(body, dict):
        return ""
    for key in ("error", "message", "detail", "error_message"):
        value = body.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("detail")
        if isinstance(value, str) and value.strip():
            reason = " ".join(value.split())[:REASON_MAX]
            return reason.replace(secret, "***") if secret else reason
    return ""


class VobizClient:
    """The Vobiz REST API, as one account."""

    def __init__(self, auth_id, auth_token, timeout=10, http=None):
        if not AUTH_ID_RE.match(auth_id or ""):
            raise VobizError("That is not a Vobiz Auth ID", status=401)
        if not auth_token:
            raise VobizError("No Vobiz Auth Token is set", status=401)
        self.auth_id = auth_id
        self._auth_token = auth_token
        self.timeout = timeout
        self._http = http or requests

    def __repr__(self):
        # Never the token, even in a traceback.
        return f"<VobizClient {self.auth_id}>"

    def request(self, method, path, payload=None):
        url = f"{API_BASE}/api/v1/Account/{quote(self.auth_id, safe='')}{path}"
        headers = {
            "X-Auth-ID": self.auth_id,
            "X-Auth-Token": self._auth_token,
            "Accept": "application/json",
        }
        try:
            response = self._http.request(method, url, json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise VobizError(f"Could not reach Vobiz ({type(exc).__name__})", status=0) from None
        body = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
        if not 200 <= response.status_code < 300:
            reason = error_reason(body, self._auth_token)
            message = f"Vobiz returned {response.status_code} for {method} {path.split('?')[0]}"
            raise VobizError(
                f"{message}: {reason}" if reason else message,
                status=response.status_code,
                reason=reason or None,
            )
        return body

    # -- numbers ----------------------------------------------------------

    def list_numbers(self):
        """The account's active voice numbers: [{"e164", "application_id"}].

        Pages through until ``total`` is reached. The first page doubles as the
        credential check: a refused Auth ID and Auth Token raise with status 401.
        """
        numbers, seen = [], set()
        for page in range(1, 51):
            body = self.request("GET", f"/numbers?page={page}&per_page=100") or {}
            items = body.get("items") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("status") not in (None, "active") or item.get("voice_enabled") is False:
                    continue
                e164 = to_e164(item.get("e164") or item.get("number"))
                if not e164 or e164 in seen:
                    continue
                seen.add(e164)
                app_id = item.get("application_id")
                numbers.append({
                    "e164": e164,
                    "application_id": str(app_id) if app_id not in (None, "") and DIGITS_RE.match(str(app_id)) else None,
                })
            try:
                total = int(body.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if not items or total <= page * 100:
                break
        return numbers

    def attach_number(self, e164, app_id):
        """Point a number's inbound calls at an application. Moves it off any other."""
        digits = re.sub(r"\D", "", e164 or "")
        if not digits or not DIGITS_RE.match(str(app_id or "")):
            raise VobizError("A number and an application id are both needed", status=400)
        self.request("POST", f"/numbers/%2B{digits}/application", {"application_id": str(app_id)})

    # -- applications -----------------------------------------------------

    def _list(self, path):
        """Every object of one of Vobiz's paged lists. Vobiz answers at most 20
        per page whatever ``limit`` asks for (seen live: limit=100 returned 20
        of 66 applications), so this pages by what each answer says."""
        found, offset = [], 0
        for _page in range(100):
            body = self.request("GET", f"{path}?limit={PAGE_SIZE}&offset={offset}") or {}
            objects = [o for o in (body.get("objects") or []) if isinstance(o, dict)]
            found.extend(objects)
            offset += len(objects)
            total = (body.get("meta") or {}).get("total_count")
            if not objects or (isinstance(total, int) and offset >= total) or (
                    not isinstance(total, int) and len(objects) < PAGE_SIZE):
                break
        return found

    def applications(self):
        """Every application in the account: [{"app_id", "app_name", "answer_url"}]."""
        return self._list("/Application/")

    # -- SIP endpoints --------------------------------------------------

    def endpoints(self):
        """The account's SIP endpoints: [{"endpoint_id", "username", "alias", "app_id"}].
        ``app_id`` is the application the endpoint's calls go to, or ""."""
        found = []
        for o in self._list("/Endpoint/"):
            endpoint_id, username = str(o.get("endpoint_id") or ""), str(o.get("username") or "")
            if DIGITS_RE.match(endpoint_id) and SIP_USER_RE.match(username):
                found.append({
                    "endpoint_id": endpoint_id,
                    "username": username,
                    "alias": str(o.get("alias") or ""),
                    "app_id": app_id_of(o.get("application")),
                })
        return found

    def endpoint_app_id(self, endpoint_id):
        """The application an endpoint's calls go to now, or ""."""
        if not DIGITS_RE.match(str(endpoint_id or "")):
            raise VobizError("An endpoint id is needed", status=400)
        body = self.request("GET", f"/Endpoint/{endpoint_id}/") or {}
        return app_id_of(body.get("application"))

    def link_endpoint(self, endpoint_id, app_id):
        """Send the calls placed from this endpoint to the application ``app_id``."""
        if not DIGITS_RE.match(str(endpoint_id or "")) or not DIGITS_RE.match(str(app_id or "")):
            raise VobizError("An endpoint id and an application id are needed", status=400)
        self.request("POST", f"/Endpoint/{endpoint_id}/", {"app_id": str(app_id)})

    def ensure_application(self, answer_url, hangup_url, app_name):
        """This Odoo's application in the account, created on first use.

        Matched on the answer URL, so one Odoo database never creates a second
        one, and an application someone else made is never repointed.
        App ids are 17 digits: they stay strings end to end.
        """
        for app in self.applications():
            if app.get("answer_url") == answer_url and DIGITS_RE.match(str(app.get("app_id") or "")):
                return str(app["app_id"])
        created = self.request("POST", "/Application/", {
            "app_name": app_name,
            "answer_url": answer_url,
            "answer_method": "POST",
            "hangup_url": hangup_url,
            "hangup_method": "POST",
        }) or {}
        app_id = str(created.get("app_id") or "")
        if not DIGITS_RE.match(app_id):
            raise VobizError("Vobiz did not return an application id", status=502)
        return app_id

    def update_application(self, app_id, answer_url, hangup_url, app_name=None):
        """Point this Odoo's own application at a new address, renamed to match
        when a name is given. The numbers attached to it keep ringing Odoo.
        Raises with status 404 if it is gone."""
        if not DIGITS_RE.match(str(app_id or "")):
            raise VobizError("An application id is needed", status=400)
        payload = {
            "answer_url": answer_url,
            "answer_method": "POST",
            "hangup_url": hangup_url,
            "hangup_method": "POST",
        }
        if app_name:
            payload["app_name"] = app_name
        self.request("POST", f"/Application/{app_id}/", payload)

    # -- access tokens ----------------------------------------------------

    def mint_access_token(self, sub, app_id, ttl=JWT_TTL_SECONDS):
        """Have Vobiz mint a short-lived access token for the identity ``<sub>_<auth_id>``.

        Two things were learned on production. Without ``app`` the registrar
        refuses the token (10001 INVALID_ACCESS_TOKEN), and with ``nbf`` = now it
        refuses it as not valid yet (10005), because its clock runs a little
        behind: a minute of slack fixes that. ``app`` also decides whose answer
        URL Vobiz fetches for this identity's calls.
        """
        if not SUB_RE.match(sub or ""):
            raise VobizError("Refusing to mint a token for a malformed identity", status=400)
        if not DIGITS_RE.match(str(app_id or "")):
            raise VobizError("Refusing to mint a token without this Odoo's application id", status=400)
        nbf = int(time.time()) - 60
        exp = nbf + int(ttl)
        body = self.request("POST", "/JWT/Token/", {
            "iss": self.auth_id,
            "sub": sub,
            "nbf": nbf,
            "exp": exp,
            "app": str(app_id),
            "per": {"voice": {"incoming_allow": True, "outgoing_allow": True}},
        }) or {}
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not JWT_SHAPE_RE.match(token):
            raise VobizError("Vobiz did not return a usable access token", status=502)
        return token, exp

    # -- calls ------------------------------------------------------------

    def place_call(self, from_e164, to_e164, answer_url, hangup_url):
        """Place a call over the REST API. The API takes E.164 digits without the +."""
        body = self.request("POST", "/Call/", {
            "from": from_e164.lstrip("+"),
            "to": to_e164.lstrip("+"),
            "answer_url": answer_url,
            "answer_method": "POST",
            "hangup_url": hangup_url,
            "hangup_method": "POST",
        }) or {}
        uuid = body.get("request_uuid") or body.get("call_uuid") or ""
        return str(uuid) if isinstance(uuid, (str, int)) else ""

    def hangup_call(self, call_uuid):
        if call_uuid:
            self.request("DELETE", f"/Call/{quote(str(call_uuid), safe='')}/")


# -- webhook signatures ---------------------------------------------------

def signature_valid(headers, url, tokens):
    """Whether a webhook was signed by Vobiz with one of ``tokens``.

    Vobiz signs the callback URL with its query string removed, and a nonce.
    Never the body, which is form-encoded:

        V3 = base64(HMAC-SHA256(auth_token, url + "." + nonce))
        V2 = base64(HMAC-SHA256(auth_token, url + nonce))

    Returns True or False, or None when the request carries no signature at all.
    """
    for sig_header, nonce_header, sep in (
        ("X-Vobiz-Signature-V3", "X-Vobiz-Signature-V3-Nonce", "."),
        ("X-Vobiz-Signature-V2", "X-Vobiz-Signature-V2-Nonce", ""),
    ):
        signature = headers.get(sig_header)
        nonce = headers.get(nonce_header)
        if not signature or not nonce:
            continue
        message = f"{url}{sep}{nonce}".encode()
        for token in tokens:
            if not token:
                continue
            expected = base64.b64encode(hmac.new(token.encode(), message, hashlib.sha256).digest()).decode()
            if hmac.compare_digest(expected, str(signature)):
                return True
        return False
    return None


def sign(url, nonce, token):
    """A V3 signature, as Vobiz would compute it. Used by the tests."""
    digest = hmac.new(token.encode(), f"{url}.{nonce}".encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


# -- phone numbers --------------------------------------------------------

def to_e164(raw, default_country_code="91"):
    """A phone number as +E.164, or "" when it cannot be one.

    "+91 98765-43210" and "0091…" keep their country code; "0XXXXXXXXXX" and a
    bare ten-digit number are taken as national numbers of the default country.
    """
    s = re.sub(r"[^\d+]", "", str(raw or ""))
    if not s:
        return ""
    if s.startswith("+"):
        digits = re.sub(r"\D", "", s)
    elif s.startswith("00"):
        digits = s[2:]
    elif len(s) == 11 and s.startswith("0"):
        digits = default_country_code + s[1:]
    elif len(s) == 10:
        digits = default_country_code + s
    else:
        digits = s
    return f"+{digits}" if 8 <= len(digits) <= 15 else ""


def sip_user(uri):
    """The user part of "sip:user@host" (or of a bare "user@host")."""
    match = re.match(r"^(?:sips?:)?([^@;>]+)", str(uri or "").strip().strip("<"), re.I)
    return match.group(1) if match else ""


# -- XML ------------------------------------------------------------------

def xml_escape(value):
    return (str(value)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def xml_doc(inner=""):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>{inner}</Response>'


# -- secrets at rest --------------------------------------------------------

def _fernet(secret):
    if not secret:
        raise ValueError("this database has no secret to encrypt with")
    key = base64.urlsafe_b64encode(hashlib.sha256(b"vobiz_calling:" + secret.encode()).digest())
    return Fernet(key)


def encrypt(secret, plaintext):
    """Encrypt an Auth Token with a key derived from the database's own secret."""
    return _fernet(secret).encrypt(plaintext.encode()).decode()


def decrypt(secret, ciphertext):
    """The Auth Token back, or "" when there is none or it cannot be read."""
    if not ciphertext:
        return ""
    try:
        return _fernet(secret).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError):
        return ""
