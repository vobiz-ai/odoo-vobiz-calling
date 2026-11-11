import json
import re
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from odoo.exceptions import AccessError, UserError
from odoo.modules.neutralize import get_neutralization_queries
from odoo.tests import HttpCase, new_test_user, tagged

from ..tools import vobiz

PUBLIC = "https://odoo.test"
AUTH_ID = "MA_TEST0001"
TOKEN = "company-token"
OUR_NUMBER = "+910000000001"
CALLER = "+910000000101"


class FakeVobiz:
    """The parts of the Vobiz REST API this module uses, recorded."""

    def __init__(self):
        self.requests = []
        self.refused = set()
        self.apps = []
        self.app_error = None
        self.numbers = [
            {"e164": OUR_NUMBER, "status": "active", "application_id": None},
            {"e164": "+910000000002", "status": "active", "application_id": "99"},
            {"e164": "+910000000003", "status": "inactive"},
        ]
        # As Vobiz lists them: "application" is a resource URI, or null.
        self.endpoints = [
            {"endpoint_id": "100000000000001", "username": "agent_desk_01", "alias": "Desk 1",
             "application": f"/v1/Account/{AUTH_ID}/Application/99/"},
            {"endpoint_id": "100000000000002", "username": "agent_desk_02", "alias": "Desk 2", "application": None},
        ]

    @staticmethod
    def page(path, objects):
        """Like Vobiz: 20 per page at most, whatever the limit asked for."""
        offset = int(dict(parse_qsl(urlsplit(path).query)).get("offset", 0))
        return {"meta": {"limit": 20, "offset": offset, "total_count": len(objects)},
                "objects": objects[offset:offset + 20]}

    def request(self, client, method, path, payload=None):
        self.requests.append((client.auth_id, method, path, payload))
        if client.auth_id in self.refused:
            raise vobiz.VobizError("Vobiz returned 401 for GET /numbers", status=401)
        if method == "GET" and path.startswith("/numbers?"):
            return {"items": self.numbers, "total": len(self.numbers)}
        if method == "GET" and path.startswith("/Application/?"):
            return self.page(path, self.apps)
        if method == "GET" and path.startswith("/Endpoint/?"):
            return self.page(path, self.endpoints)
        one = re.match(r"^/Endpoint/(\d+)/$", path)
        if one:
            endpoint = next((e for e in self.endpoints if e["endpoint_id"] == one.group(1)), None)
            if not endpoint:
                raise vobiz.VobizError("Vobiz returned 404 for /Endpoint/", status=404)
            if method == "POST":
                endpoint["application"] = f"/v1/Account/{AUTH_ID}/Application/{payload['app_id']}/"
                return {"message": "changed"}
            return dict(endpoint)
        if method == "POST" and path == "/Application/":
            if self.app_error:
                raise self.app_error
            app = {"app_id": "12345678901234567", **payload}
            self.apps.append(app)
            return {"app_id": app["app_id"]}
        updated = re.match(r"^/Application/(\d+)/$", path)
        if method == "POST" and updated:
            app = next((a for a in self.apps if a["app_id"] == updated.group(1)), None)
            if not app:
                raise vobiz.VobizError("Vobiz returned 404 for POST /Application/", status=404)
            app.update(payload)
            return {"message": "changed"}
        if method == "POST" and path == "/JWT/Token/":
            return {"token": "header.payload.signature"}
        if method == "POST" and path == "/Call/":
            return {"request_uuid": "customer-leg-1"}
        if method == "POST" and path.endswith("/application"):
            return {}
        if method == "DELETE":
            return None
        raise AssertionError(f"unexpected Vobiz call {method} {path}")


@tagged("post_install", "-at_install")
class TestVobizWebhooks(HttpCase):

    def setUp(self):
        super().setUp()
        self.fake = FakeVobiz()
        patcher = patch.object(vobiz.VobizClient, "request", autospec=True, side_effect=self.fake.request)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env["ir.config_parameter"].sudo().set_param("vobiz_calling.public_url", PUBLIC)
        company = self.env.company.sudo()
        company.vobiz_auth_id = AUTH_ID
        company._vobiz_set_auth_token(TOKEN)
        company.vobiz_default_number = OUR_NUMBER
        company.vobiz_app_id = False
        company.vobiz_app_url = False
        company.vobiz_data_consent = True
        self.partner = self.env["res.partner"].create({"name": "Emily Garcia", "phone": "+91 00000-00101"})
        self.agent = new_test_user(self.env, login="agent1", password="agent1-password", groups="base.group_user")

    # -- helpers -------------------------------------------------------------

    def post(self, path, data, token=TOKEN, signed=True):
        headers = {}
        if signed:
            nonce = f"nonce-{len(self.fake.requests)}-{path}"
            url = PUBLIC + path.split("?")[0]
            headers = {"X-Vobiz-Signature-V3": vobiz.sign(url, nonce, token), "X-Vobiz-Signature-V3-Nonce": nonce}
        return self.url_open(path, data=data, headers=headers)

    def sign_in(self, login="agent1", password="agent1-password", **params):
        self.authenticate(login, password)
        return self.make_jsonrpc_request("/vobiz/panel/sign_in", {"mode": "company", **params})

    def session_of(self, user):
        return self.env["vobiz.session"].sudo().search([("user_id", "=", user.id), ("active", "=", True)])

    def sign_in_error(self, mode="company", login="agent1", password="agent1-password", **params):
        """The message the panel would show for a refused sign-in. make_jsonrpc_request
        keeps only the error's class name; the panel shows error.data.message."""
        self.authenticate(login, password)
        response = self.url_open("/vobiz/panel/sign_in", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 0, "params": {"mode": mode, **params},
        }), headers={"Content-Type": "application/json"})
        error = response.json()["error"]["data"]
        self.assertEqual(error["name"], "odoo.exceptions.UserError")
        return error["message"]

    # -- refusals ------------------------------------------------------------

    def test_unsigned_or_forged_webhooks_are_refused(self):
        Call = self.env["vobiz.call"].sudo()
        before = Call.search([]).ids
        inbound = {"From": CALLER, "To": OUR_NUMBER, "CallUUID": "a-1", "ParentAuthID": AUTH_ID}
        self.assertEqual(self.post("/vobiz/answer", inbound, signed=False).status_code, 403)
        self.assertEqual(self.post("/vobiz/answer", inbound, token="not-the-token").status_code, 403)
        # An account this Odoo holds no token for cannot be vouched for.
        other = dict(inbound, ParentAuthID="MA_TEST9999")
        self.assertEqual(self.post("/vobiz/answer", other, token="whatever").status_code, 403)
        # Calls logged before this test (a real database may hold some) do not count.
        self.assertFalse(Call.search([("id", "not in", before)]))

    # -- inbound ---------------------------------------------------------------

    def test_inbound_with_nobody_signed_in_is_turned_away_as_busy_and_logged(self):
        response = self.post("/vobiz/answer", {"From": CALLER, "To": OUR_NUMBER, "CallUUID": "a-1",
                                               "ParentAuthID": AUTH_ID})
        self.assertEqual(response.status_code, 200)
        self.assertIn('<Hangup reason="busy"/>', response.text)
        call = self.env["vobiz.call"].sudo().search([("call_uuid", "=", "a-1")])
        self.assertEqual((call.direction, call.outcome, call.partner_id), ("inbound", "missed", self.partner))
        self.assertTrue(call.posted)
        self.assertIn("Vobiz call", self.partner.message_ids[:1].body)

    def test_inbound_rings_the_signed_in_agents_then_logs_the_answered_call(self):
        self.sign_in()
        session = self.session_of(self.agent)
        response = self.post("/vobiz/answer", {"From": CALLER, "To": OUR_NUMBER, "CallUUID": "a-2",
                                               "ParentAuthID": AUTH_ID})
        xml = response.text
        self.assertIn(f"<User>sip:{session.identity}@registrar.vobiz.ai</User>", xml)
        self.assertIn(f'callerId="{OUR_NUMBER}"', xml)
        self.assertIn(f'callerName="{CALLER}"', xml)
        self.assertIn('redirect="true"', xml)
        self.assertIn(f'action="{PUBLIC}/vobiz/dial-status"', xml)
        self.assertNotIn("<Hangup", xml)

        self.post("/vobiz/dial-event", {"Event": "DialAnswer", "DialALegUUID": "a-2",
                                        "DialBLegTo": f"sip:{session.identity}@registrar.vobiz.ai"})
        call = self.env["vobiz.call"].sudo().search([("call_uuid", "=", "a-2")])
        self.assertEqual((call.state, call.user_id), ("in_progress", self.agent))

        done = self.post("/vobiz/dial-status", {"CallUUID": "a-2", "DialStatus": "completed",
                                                "CallStatus": "in-progress"})
        self.assertIn("<Hangup/>", done.text)
        self.post("/vobiz/hangup", {"CallUUID": "a-2", "HangupCause": "NORMAL_CLEARING"})
        self.assertEqual((call.state, call.outcome), ("done", "answered"))
        self.assertTrue(call.posted)

    def test_a_caller_nobody_answers_hears_busy(self):
        self.sign_in()
        self.post("/vobiz/answer", {"From": CALLER, "To": OUR_NUMBER, "CallUUID": "a-3", "ParentAuthID": AUTH_ID})
        # A panel's Decline arrives as completed with the caller still ringing.
        declined = self.post("/vobiz/dial-status", {"CallUUID": "a-3", "DialStatus": "completed",
                                                    "CallStatus": "ringing"})
        self.assertIn('<Hangup reason="busy"/>', declined.text)
        self.assertEqual(self.env["vobiz.call"].sudo().search([("call_uuid", "=", "a-3")]).outcome, "declined")

        self.post("/vobiz/answer", {"From": CALLER, "To": OUR_NUMBER, "CallUUID": "a-4", "ParentAuthID": AUTH_ID})
        rang_out = self.post("/vobiz/dial-status", {"CallUUID": "a-4", "DialStatus": "no-answer",
                                                    "CallStatus": "ringing"})
        self.assertIn('<Hangup reason="busy"/>', rang_out.text)
        self.assertEqual(self.env["vobiz.call"].sudo().search([("call_uuid", "=", "a-4")]).outcome, "no_answer")

    # -- outbound --------------------------------------------------------------

    def test_outbound_runs_through_a_room_and_logs_the_call(self):
        self.sign_in()
        session = self.session_of(self.agent)
        response = self.post("/vobiz/answer", {
            "From": f"sip:{session.identity}@registrar.vobiz.ai", "To": CALLER, "CallUUID": "agent-leg-1",
            "X-VH-Caller-ID": "+910000000002", "X-VH-Call-Ref": "ab" * 12})
        self.assertIn("<Conference", response.text)
        placed = [r for r in self.fake.requests if r[1:3] == ("POST", "/Call/")]
        self.assertEqual(len(placed), 1)
        self.assertEqual((placed[0][3]["from"], placed[0][3]["to"]), ("910000000002", "910000000101"))
        room = self.env["vobiz.room"].sudo().search([("agent_call_uuid", "=", "agent-leg-1")])
        self.assertTrue(room)
        self.assertIn(f"room={room.name}", placed[0][3]["answer_url"])

        joined = self.post(f"/vobiz/outbound-join?room={room.name}", {"CallUUID": "customer-leg-1"})
        self.assertIn('startConferenceOnEnter="true"', joined.text)
        self.assertEqual(room.call_id.state, "in_progress")
        self.post(f"/vobiz/outbound-hangup?room={room.name}", {"CallUUID": "customer-leg-1",
                                                              "HangupCause": "NORMAL_CLEARING"})
        call = room.call_id
        self.assertEqual((call.direction, call.outcome, call.partner_id, call.user_id),
                         ("outbound", "answered", self.partner, self.agent))

    def test_an_unanswered_outbound_call_releases_the_agent(self):
        self.sign_in()
        session = self.session_of(self.agent)
        self.post("/vobiz/answer", {"From": f"sip:{session.identity}@registrar.vobiz.ai", "To": CALLER,
                                    "CallUUID": "agent-leg-2"})
        room = self.env["vobiz.room"].sudo().search([("agent_call_uuid", "=", "agent-leg-2")])
        self.post(f"/vobiz/outbound-hangup?room={room.name}", {"HangupCause": "NO_ANSWER"})
        self.assertEqual(room.call_id.outcome, "no_answer")
        self.assertIn(("MA_TEST0001", "DELETE", "/Call/agent-leg-2/", None), self.fake.requests)

    def start_outbound(self, agent_leg):
        """The agent's browser calls CALLER. Returns the call's room."""
        self.sign_in()
        session = self.session_of(self.agent)
        self.post("/vobiz/answer", {"From": f"sip:{session.identity}@registrar.vobiz.ai", "To": CALLER,
                                    "CallUUID": agent_leg})
        return self.env["vobiz.room"].sudo().search([("agent_call_uuid", "=", agent_leg)])

    def test_hanging_up_before_the_customer_answers_cancels_their_call(self):
        room = self.start_outbound("agent-leg-4")
        self.assertEqual(room.customer_call_uuid, "customer-leg-1")
        # The agent hangs up while the customer's phone still rings.
        self.post("/vobiz/hangup", {"CallUUID": "agent-leg-4", "HangupCause": "NORMAL_CLEARING"})
        self.assertIn((AUTH_ID, "DELETE", "/Call/customer-leg-1/", None), self.fake.requests)
        self.assertEqual(room.call_id.outcome, "canceled")
        # The cancelled leg's own hang-up follows: nothing is released twice.
        before = len(self.fake.requests)
        self.post(f"/vobiz/outbound-hangup?room={room.name}", {"CallUUID": "customer-leg-1",
                                                              "HangupCause": "ORIGINATOR_CANCEL"})
        self.assertFalse([r for r in self.fake.requests[before:] if r[1] == "DELETE"])
        self.assertEqual(room.call_id.outcome, "canceled")

    def test_a_customer_who_answers_after_the_agent_left_is_hung_up(self):
        room = self.start_outbound("agent-leg-5")
        self.post("/vobiz/hangup", {"CallUUID": "agent-leg-5", "HangupCause": "NORMAL_CLEARING"})
        joined = self.post(f"/vobiz/outbound-join?room={room.name}", {"CallUUID": "customer-leg-1"})
        self.assertIn("<Hangup/>", joined.text)
        self.assertNotIn("<Conference", joined.text)
        self.assertEqual((room.call_id.outcome, room.call_id.answer_date), ("canceled", False))

    def test_a_declined_call_releases_the_agent_and_cancels_nothing(self):
        # The customer declines: their leg ends first, and the agent is released.
        room = self.start_outbound("agent-leg-6")
        self.post(f"/vobiz/outbound-hangup?room={room.name}", {"CallUUID": "customer-leg-1",
                                                              "HangupCause": "USER_BUSY"})
        self.assertIn((AUTH_ID, "DELETE", "/Call/agent-leg-6/", None), self.fake.requests)
        before = len(self.fake.requests)
        self.post("/vobiz/hangup", {"CallUUID": "agent-leg-6", "HangupCause": "NORMAL_CLEARING"})
        self.assertFalse([r for r in self.fake.requests[before:] if r[1] == "DELETE"])
        self.assertEqual(room.call_id.outcome, "busy")

    def test_an_unknown_caller_id_falls_back_to_the_session_number(self):
        self.sign_in()
        session = self.session_of(self.agent)
        self.post("/vobiz/answer", {"From": f"sip:{session.identity}@registrar.vobiz.ai", "To": CALLER,
                                    "CallUUID": "agent-leg-3", "X-VH-Caller-ID": "+919999999999"})
        placed = [r for r in self.fake.requests if r[1:3] == ("POST", "/Call/")][-1]
        self.assertEqual(placed[3]["from"], OUR_NUMBER.lstrip("+"))

    # -- sign-in ---------------------------------------------------------------

    def test_the_panel_never_receives_the_auth_token(self):
        state = self.sign_in()
        self.assertTrue(state["signedIn"])
        self.assertEqual(state["accessToken"], "header.payload.signature")
        self.assertNotIn(TOKEN, str(state))
        self.assertEqual(state["numbers"], [OUR_NUMBER, "+910000000002"])
        self.assertEqual(state["callerId"], OUR_NUMBER)
        minted = [r for r in self.fake.requests if r[2] == "/JWT/Token/"][-1][3]
        self.assertEqual(minted["app"], "12345678901234567")
        self.assertIsInstance(minted["app"], str)
        self.assertLess(minted["nbf"], minted["exp"])

    def test_signing_in_again_ends_the_previous_sign_in(self):
        self.sign_in()
        first = self.session_of(self.agent)
        self.sign_in()
        second = self.session_of(self.agent)
        self.assertNotEqual(first, second)
        self.assertFalse(first.active)
        outbound = self.post("/vobiz/answer", {"From": f"sip:{first.identity}@registrar.vobiz.ai", "To": CALLER,
                                               "CallUUID": "stale", "ParentAuthID": AUTH_ID})
        self.assertIn("<Hangup/>", outbound.text)

    def test_an_own_account_is_encrypted_and_forgotten_at_sign_out(self):
        state = self.sign_in(mode="own", auth_id="MA_TEST0002", auth_token="own-token")
        self.assertEqual((state["mode"], state["authId"]), ("own", "MA_TEST0002"))
        session = self.session_of(self.agent)
        self.assertTrue(session.auth_token_enc)
        self.assertNotIn("own-token", session.auth_token_enc)
        # Its webhooks verify with the agent's own token.
        response = self.post("/vobiz/answer", {"From": CALLER, "To": OUR_NUMBER, "CallUUID": "own-1",
                                               "ParentAuthID": "MA_TEST0002"}, token="own-token")
        self.assertEqual(response.status_code, 200)
        self.make_jsonrpc_request("/vobiz/panel/sign_out", {})
        self.assertFalse(session.active)
        self.assertFalse(session.auth_token_enc)

    def test_refused_credentials_say_so(self):
        self.fake.refused.add("MA_TEST0002")
        self.authenticate("agent1", "agent1-password")
        # make_jsonrpc_request keeps only the error's class name; the panel shows
        # error.data.message, so read that.
        response = self.url_open("/vobiz/panel/sign_in", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 0,
            "params": {"mode": "own", "auth_id": "MA_TEST0002", "auth_token": "wrong"},
        }), headers={"Content-Type": "application/json"})
        error = response.json()["error"]["data"]
        self.assertEqual(error["name"], "odoo.exceptions.UserError")
        self.assertIn("did not accept", error["message"])
        self.assertNotIn("wrong", error["message"])
        self.assertFalse(self.session_of(self.agent))

    def test_only_administrators_can_read_the_stored_token(self):
        company = self.env.company.with_user(self.agent)
        with self.assertRaises(AccessError):
            company.read(["vobiz_auth_token_enc"])

    def test_the_contact_matches_however_the_number_was_typed(self):
        Call = self.env["vobiz.call"]
        self.assertEqual(Call._match_partner(CALLER), self.partner)
        self.assertFalse(Call._match_partner("+910000000999"))

    # -- setup safeguards (from the live test of 9 Nov) ------------------------

    def test_sign_in_needs_the_administrators_consent(self):
        self.env.company.sudo().vobiz_data_consent = False
        self.assertIn("not allowed yet", self.sign_in_error("company"))
        self.assertIn("not allowed yet", self.sign_in_error("own", auth_id="MA_TEST0002", auth_token="own-token"))
        self.assertFalse(self.session_of(self.agent))
        self.assertFalse(self.fake.requests, "Vobiz is not contacted before the opt-in")

    def test_sign_in_needs_an_https_public_url_in_both_modes(self):
        self.env["ir.config_parameter"].sudo().set_param("vobiz_calling.public_url", "http://odoo.test")
        self.assertIn("over HTTPS", self.sign_in_error("company"))
        self.assertIn("over HTTPS", self.sign_in_error("own", auth_id="MA_TEST0002", auth_token="own-token"))
        self.assertFalse(self.fake.requests)

    def test_vobiz_refusing_this_odoos_address_says_why(self):
        self.fake.app_error = vobiz.VobizError(
            "Vobiz returned 400 for POST /Application/: answer_url is not a valid URL",
            status=400, reason="answer_url is not a valid URL")
        message = self.sign_in_error("company")
        self.assertIn("cannot use this Odoo's address", message)
        self.assertIn("answer_url is not a valid URL", message)
        self.assertIn(PUBLIC, message)

    def test_moving_the_public_url_moves_the_application(self):
        self.sign_in()
        company = self.env.company.sudo()
        app_id = company.vobiz_app_id
        self.assertEqual(company.vobiz_app_url, PUBLIC)
        on_company = self.session_of(self.agent)
        own_user = new_test_user(self.env, login="agent2", password="agent2-password", groups="base.group_user")
        self.sign_in(login="agent2", password="agent2-password", mode="own",
                     auth_id="MA_TEST0002", auth_token="own-token")
        on_own = self.session_of(own_user)

        self.env["res.config.settings"].create({"vobiz_public_url": "https://moved.test"}).execute()

        updates = [r for r in self.fake.requests if r[1:3] == ("POST", f"/Application/{app_id}/")]
        self.assertEqual(updates[-1][3]["answer_url"], "https://moved.test/vobiz/answer")
        self.assertEqual(updates[-1][3]["hangup_url"], "https://moved.test/vobiz/hangup")
        # Renamed too, so the Vobiz Console doesn't show the old address.
        self.assertEqual(updates[-1][3]["app_name"], "Odoo-Calling (moved.test)")
        # Same application, so its numbers and the company's agents follow.
        self.assertEqual((company.vobiz_app_id, company.vobiz_app_url), (app_id, "https://moved.test"))
        self.assertTrue(on_company.active)
        # An own account's application was made for the old address: signed out.
        self.assertFalse(on_own.active)

    def test_a_deleted_application_is_made_again(self):
        company = self.env.company.sudo()
        company.write({"vobiz_app_id": "11111111111111111", "vobiz_app_url": "https://old.test"})
        state = self.sign_in()
        self.assertTrue(state["signedIn"])
        self.assertEqual(company.vobiz_app_id, "12345678901234567")
        self.assertEqual(company.vobiz_app_url, PUBLIC)

    def test_the_caller_id_is_picked_from_the_account_numbers(self):
        company = self.env.company.sudo()
        company.vobiz_numbers = json.dumps([OUR_NUMBER, "+910000000002"])
        Settings = self.env["res.config.settings"]
        self.assertEqual([value for value, _label in Settings._vobiz_caller_id_selection()],
                         [OUR_NUMBER, "+910000000002"])
        company.vobiz_default_number = "+919999999999"  # an agent's own mobile, not on the account
        self.assertFalse(Settings.create({}).vobiz_caller_id)
        company.vobiz_default_number = OUR_NUMBER
        self.assertEqual(Settings.create({}).vobiz_caller_id, OUR_NUMBER)
        Settings.create({"vobiz_caller_id": "+910000000002"}).execute()
        self.assertEqual(company.vobiz_default_number, "+910000000002")

    def test_connected_only_after_a_real_check(self):
        # Turning on incoming calls sets up the application, but is not the check:
        # the status must not say "Connected" until Save and check connection ran.
        Settings = self.env["res.config.settings"]
        Settings.create({}).action_vobiz_enable_inbound()
        company = self.env.company.sudo()
        self.assertTrue(company.vobiz_app_id)
        self.assertEqual(json.loads(company.vobiz_numbers), [OUR_NUMBER, "+910000000002"])
        self.assertEqual(Settings.create({}).vobiz_status_kind, "warning")
        target = "odoo.addons.vobiz_calling.models.vobiz_session.VobizSession._public_url_reaches_us"
        with patch(target, return_value=(True, "")):
            Settings.create({}).action_vobiz_check()
        status = Settings.create({})
        self.assertEqual(status.vobiz_status_kind, "success")
        self.assertIn("At the last check", status.vobiz_status)
        # The time is a date field, shown in the browser's time zone, not text.
        self.assertEqual(status.vobiz_checked_on, company.vobiz_checked_on)
        # The check writes the application's addresses and name again, even unmoved.
        resynced = [r for r in self.fake.requests if r[1:3] == ("POST", f"/Application/{company.vobiz_app_id}/")]
        self.assertEqual(resynced[-1][3]["app_name"], "Odoo-Calling (odoo.test)")
        self.assertEqual(resynced[-1][3]["answer_url"], f"{PUBLIC}/vobiz/answer")
        # A check whose call to this Odoo failed is not "Connected".
        with patch(target, return_value=(False, "no answer")):
            Settings.create({}).action_vobiz_check()
        status = Settings.create({})
        self.assertEqual(status.vobiz_status_kind, "warning")
        self.assertIn("did not answer", status.vobiz_status)
        # Nor is an address saved with the top Save, without a check.
        with patch(target, return_value=(True, "")):
            Settings.create({}).action_vobiz_check()
        Settings.create({"vobiz_public_url": "https://moved.test"}).execute()
        status = Settings.create({})
        self.assertEqual(status.vobiz_status_kind, "warning")
        self.assertIn("Not checked for this address yet", status.vobiz_status)

    def test_the_panel_can_ask_whether_this_odoo_is_reachable(self):
        target = "odoo.addons.vobiz_calling.models.vobiz_session.VobizSession._public_url_reaches_us"
        self.authenticate("agent1", "agent1-password")
        # Not signed in to the phone: nothing is checked.
        with patch(target, return_value=(False, "no answer")) as check:
            self.assertEqual(self.make_jsonrpc_request("/vobiz/panel/check_reach", {}), {})
        check.assert_not_called()
        self.sign_in()
        with patch(target, return_value=(False, "no answer: ConnectTimeout")):
            result = self.make_jsonrpc_request("/vobiz/panel/check_reach", {})
        self.assertEqual(result, {"reached": False, "url": PUBLIC, "why": "no answer: ConnectTimeout"})

    def test_the_address_self_check(self):
        nonce = "ab" * 16
        answer = self.url_open(f"/vobiz/ping?nonce={nonce}")
        self.assertEqual(answer.status_code, 200)
        Session = self.env["vobiz.session"]
        self.assertEqual(answer.text, Session._ping_answer(nonce))
        self.assertEqual(self.url_open("/vobiz/ping?nonce=not-a-nonce").status_code, 400)

        class Reply:
            def __init__(self, status, text):
                self.status_code, self.text = status, text

        target = "odoo.addons.vobiz_calling.models.vobiz_session.requests.get"
        with patch(target, side_effect=lambda url, params, **kw: Reply(200, Session._ping_answer(params["nonce"]))):
            self.assertEqual(Session._public_url_reaches_us(), (True, ""))
        with patch(target, return_value=Reply(200, "some other server")):
            self.assertFalse(Session._public_url_reaches_us()[0])
        with patch(target, return_value=Reply(530, "")):
            self.assertFalse(Session._public_url_reaches_us()[0])

    # -- paged lists -------------------------------------------------------------

    def test_applications_are_read_page_by_page(self):
        # Vobiz answers 20 per page whatever the limit; this Odoo's application is on page 2.
        self.fake.apps = [{"app_id": str(10**16 + i), "answer_url": f"https://other{i}.test/answer"} for i in range(22)]
        self.fake.apps.append({"app_id": "12345678901234560", "answer_url": f"{PUBLIC}/vobiz/answer"})
        self.sign_in()
        self.assertEqual(self.env.company.sudo().vobiz_app_id, "12345678901234560")
        self.assertFalse([r for r in self.fake.requests if r[1:3] == ("POST", "/Application/")])

    # -- SIP endpoints ---------------------------------------------------------------

    def allow_endpoint(self, username="agent_desk_01", user=None):
        """What an administrator does: Load from Vobiz, then Use with Odoo."""
        Endpoint = self.env["vobiz.endpoint"]
        Endpoint.action_load()
        endpoint = Endpoint.search([("username", "=", username)])
        endpoint.user_id = user
        endpoint.action_use_in_odoo()
        return endpoint

    def test_load_from_vobiz_works_as_the_button_calls_it(self):
        # The list's header button posts to call_button with no rows selected.
        new_test_user(self.env, login="boss", password="boss-password", groups="base.group_system")
        self.authenticate("boss", "boss-password")
        result = self.make_jsonrpc_request("/web/dataset/call_button/vobiz.endpoint/action_load", {
            "model": "vobiz.endpoint", "method": "action_load", "args": [[]], "kwargs": {"context": {}},
        })
        self.assertEqual(result["tag"], "display_notification")
        self.assertEqual(len(self.env["vobiz.endpoint"].search([])), 2)

    def test_endpoints_are_loaded_then_linked_to_odoo(self):
        endpoint = self.allow_endpoint()
        company = self.env.company.sudo()
        self.assertEqual(sorted(self.env["vobiz.endpoint"].search([]).mapped("username")),
                         ["agent_desk_01", "agent_desk_02"])
        self.assertIn((AUTH_ID, "POST", "/Endpoint/100000000000001/", {"app_id": company.vobiz_app_id}),
                      self.fake.requests)
        self.assertTrue(endpoint.allowed and endpoint.linked)
        self.assertFalse(self.env["vobiz.endpoint"].search([("username", "=", "agent_desk_02")]).allowed)

    def test_sip_sign_in_takes_an_allowed_endpoint_and_no_token(self):
        self.assertIn("not a SIP endpoint this Odoo may use",
                      self.sign_in_error(mode="sip", sip_username="agent_desk_01"))
        self.allow_endpoint()
        state = self.sign_in(mode="sip", sip_username="agent_desk_01")
        self.assertEqual((state["signedIn"], state["mode"], state["identity"]), (True, "sip", "agent_desk_01"))
        # The browser registers with the password it holds: no access token, and
        # Odoo never asked Vobiz for one.
        self.assertNotIn("accessToken", state)
        self.assertFalse([r for r in self.fake.requests if r[1:3] == ("POST", "/JWT/Token/")])
        self.assertEqual(self.make_jsonrpc_request("/vobiz/panel/token", {}),
                         {"signedIn": True, "identity": "agent_desk_01", "mode": "sip"})

    def test_sip_sign_in_checks_the_endpoint_still_reaches_odoo(self):
        self.allow_endpoint()
        # Linked to another application in the Vobiz Console since.
        self.fake.endpoints[0]["application"] = f"/v1/Account/{AUTH_ID}/Application/99/"
        self.assertIn("don't go to Odoo's Vobiz application",
                      self.sign_in_error(mode="sip", sip_username="agent_desk_01"))

    def test_an_endpoint_is_for_one_person_at_a_time(self):
        other = new_test_user(self.env, login="agent2", password="agent2-password", groups="base.group_user")
        endpoint = self.allow_endpoint(user=other)
        self.assertIn("kept for another Odoo user", self.sign_in_error(mode="sip", sip_username="agent_desk_01"))
        endpoint.user_id = False
        self.sign_in(login="agent2", password="agent2-password", mode="sip", sip_username="agent_desk_01")
        self.assertIn("is signed in with the SIP endpoint",
                      self.sign_in_error(mode="sip", sip_username="agent_desk_01"))

    def test_a_sip_agent_makes_and_takes_calls(self):
        self.allow_endpoint()
        self.sign_in(mode="sip", sip_username="agent_desk_01")
        outbound = self.post("/vobiz/answer", {"From": "sip:agent_desk_01@registrar.vobiz.ai", "To": CALLER,
                                               "CallUUID": "agent-leg-7"})
        self.assertIn("<Conference", outbound.text)
        placed = [r for r in self.fake.requests if r[1:3] == ("POST", "/Call/")]
        self.assertEqual(placed[-1][0], AUTH_ID)  # placed with the company's account
        inbound = self.post("/vobiz/answer", {"From": CALLER, "To": OUR_NUMBER, "CallUUID": "a-sip-1",
                                              "ParentAuthID": AUTH_ID})
        self.assertIn("<User>sip:agent_desk_01@registrar.vobiz.ai</User>", inbound.text)

    def test_signing_in_again_with_an_endpoint_frees_its_name(self):
        self.allow_endpoint()
        self.sign_in(mode="sip", sip_username="agent_desk_01")
        first = self.session_of(self.agent)
        self.sign_in(mode="sip", sip_username="agent_desk_01")
        second = self.session_of(self.agent)
        self.assertNotEqual(first, second)
        self.assertFalse(first.active)
        self.assertEqual(second.identity, "agent_desk_01")
        self.assertTrue(first.identity.startswith("agent_desk_01~"))

    def test_stopping_an_endpoint_signs_its_agent_out(self):
        endpoint = self.allow_endpoint()
        self.sign_in(mode="sip", sip_username="agent_desk_01")
        session = self.session_of(self.agent)
        endpoint.action_stop_using()
        self.assertFalse(session.active)
        self.assertFalse(endpoint.allowed)
        self.assertIn("not a SIP endpoint this Odoo may use",
                      self.sign_in_error(mode="sip", sip_username="agent_desk_01"))

    # -- neutralized copies ----------------------------------------------------

    def test_a_neutralized_copy_cannot_act_for_the_live_database(self):
        # Odoo.sh staging and development databases, and duplicates or restores
        # made with "Neutralize", run every module's data/neutralize.sql. A copy
        # that kept the live Auth Token could point the live numbers at itself.
        room = self.start_outbound("agent-leg-n1")
        call = room.call_id
        other = new_test_user(self.env, login="agent2", password="agent2-password", groups="base.group_user")
        self.sign_in(login="agent2", password="agent2-password", mode="own",
                     auth_id="MA_TEST0002", auth_token="own-token")
        company = self.env.company.sudo()
        self.assertTrue(company.vobiz_app_id)
        self.assertTrue(self.session_of(other).auth_token_enc)

        for query in get_neutralization_queries(["vobiz_calling"]):
            self.env.cr.execute(query)
        # A copy's own server starts with empty caches; here the same process
        # still holds the settings it read before the SQL ran.
        self.env.invalidate_all()
        self.env.registry.clear_cache()

        # No Auth Token, no consent, no link to the live application, no sign-ins.
        self.assertFalse(company._vobiz_configured())
        self.assertEqual(company._vobiz_auth_token(), "")
        self.assertFalse(company.vobiz_data_consent)
        self.assertFalse(company.vobiz_app_id or company.vobiz_app_url)
        self.assertFalse(self.env["ir.config_parameter"].sudo().get_param("vobiz_calling.public_url"))
        self.assertFalse(self.env["vobiz.session"].sudo().with_context(active_test=False).search([]))
        self.assertFalse(self.env["vobiz.room"].sudo().search([]))
        with self.assertRaises(UserError):
            company._vobiz_client()
        # What is not a secret stays: the account, its default number, the call history.
        self.assertEqual((company.vobiz_auth_id, company.vobiz_default_number), (AUTH_ID, OUR_NUMBER))
        self.assertTrue(call.exists())
        # Even at the live address, the copy no longer vouches for the live account's webhooks.
        self.env["ir.config_parameter"].sudo().set_param("vobiz_calling.public_url", PUBLIC)
        inbound = {"From": CALLER, "To": OUR_NUMBER, "CallUUID": "a-n1", "ParentAuthID": AUTH_ID}
        self.assertEqual(self.post("/vobiz/answer", inbound).status_code, 403)
