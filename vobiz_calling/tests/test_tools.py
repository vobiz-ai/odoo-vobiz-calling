from odoo.tests import TransactionCase, tagged

from ..tools import vobiz


@tagged("post_install", "-at_install")
class TestVobizTools(TransactionCase):

    def test_to_e164(self):
        cases = {
            "+91 98765-43210": "+919876543210",
            "0091 9876543210": "+919876543210",
            "09876543210": "+919876543210",
            "9876543210": "+919876543210",
            "919876543210": "+919876543210",
            "+1 (415) 555-0100": "+14155550100",
            "": "",
            "12": "",
            "abc": "",
        }
        for raw, expected in cases.items():
            self.assertEqual(vobiz.to_e164(raw), expected, raw)

    def test_sip_user(self):
        self.assertEqual(vobiz.sip_user("sip:odoo-abc_MA_TEST0001@registrar.vobiz.ai"), "odoo-abc_MA_TEST0001")
        self.assertEqual(vobiz.sip_user("<sip:agent@x>;tag=1"), "agent")
        self.assertEqual(vobiz.sip_user(""), "")

    def test_signature_v3_and_v2(self):
        url, nonce, token = "https://odoo.test/vobiz/answer", "12345", "secret-token"
        v3 = {"X-Vobiz-Signature-V3": vobiz.sign(url, nonce, token), "X-Vobiz-Signature-V3-Nonce": nonce}
        self.assertTrue(vobiz.signature_valid(v3, url, [token]))
        self.assertFalse(vobiz.signature_valid(v3, url, ["another-token"]))
        # The signature covers the URL: another path does not verify.
        self.assertFalse(vobiz.signature_valid(v3, "https://odoo.test/vobiz/hangup", [token]))
        self.assertIsNone(vobiz.signature_valid({}, url, [token]))
        import base64
        import hashlib
        import hmac
        v2sig = base64.b64encode(hmac.new(token.encode(), f"{url}{nonce}".encode(), hashlib.sha256).digest()).decode()
        self.assertTrue(vobiz.signature_valid(
            {"X-Vobiz-Signature-V2": v2sig, "X-Vobiz-Signature-V2-Nonce": nonce}, url, [token]))

    def test_encrypt_round_trip(self):
        ciphertext = vobiz.encrypt("db-secret", "my-auth-token")
        self.assertNotIn("my-auth-token", ciphertext)
        self.assertEqual(vobiz.decrypt("db-secret", ciphertext), "my-auth-token")
        # Another database's secret cannot read it.
        self.assertEqual(vobiz.decrypt("other-secret", ciphertext), "")
        self.assertEqual(vobiz.decrypt("db-secret", ""), "")

    def test_xml_escape(self):
        self.assertEqual(vobiz.xml_escape('a<b>&"\''), "a&lt;b&gt;&amp;&quot;&apos;")

    def test_client_refuses_a_malformed_auth_id(self):
        with self.assertRaises(vobiz.VobizError):
            vobiz.VobizClient("MA bad id", "token")
        with self.assertRaises(vobiz.VobizError):
            vobiz.VobizClient("MA_TEST0001", "")

    def test_client_repr_hides_the_token(self):
        self.assertNotIn("tok-123", repr(vobiz.VobizClient("MA_TEST0001", "tok-123")))

    def test_vobiz_reason_is_kept_and_never_carries_the_token(self):
        import json

        class Reply:
            def __init__(self, status, body):
                self.status_code, self._body = status, body
                self.content = json.dumps(body).encode()

            def json(self):
                return self._body

        class Http:
            def __init__(self, reply):
                self.reply = reply

            def request(self, *args, **kwargs):
                return self.reply

        client = vobiz.VobizClient("MA_TEST0001", "tok-123", http=Http(Reply(400, {"error": "answer_url is not a valid URL"})))
        with self.assertRaises(vobiz.VobizError) as caught:
            client.request("POST", "/Application/", {})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.reason, "answer_url is not a valid URL")
        self.assertIn("answer_url is not a valid URL", str(caught.exception))

        echo = vobiz.VobizClient("MA_TEST0001", "tok-123", http=Http(Reply(400, {"message": "bad token tok-123"})))
        with self.assertRaises(vobiz.VobizError) as caught:
            echo.request("GET", "/numbers")
        self.assertEqual(caught.exception.reason, "bad token ***")
        self.assertNotIn("tok-123", str(caught.exception))

        silent = vobiz.VobizClient("MA_TEST0001", "tok-123", http=Http(Reply(500, ["not", "a", "dict"])))
        with self.assertRaises(vobiz.VobizError) as caught:
            silent.request("GET", "/numbers")
        self.assertIsNone(caught.exception.reason)
        self.assertEqual(str(caught.exception), "Vobiz returned 500 for GET /numbers")

    def test_an_endpoints_application_id(self):
        # Vobiz sends a resource URI or null (seen live); the OpenAPI also allows an object.
        self.assertEqual(vobiz.app_id_of("/v1/Account/MA_TEST0001/Application/24954738743267070/"), "24954738743267070")
        self.assertEqual(vobiz.app_id_of({"app_id": "12345678901234567"}), "12345678901234567")
        self.assertEqual(vobiz.app_id_of(None), "")
        self.assertEqual(vobiz.app_id_of("/v1/Account/MA_TEST0001/Application/not-an-id/"), "")
