"""End-to-end tests with a fake network. Run: python -m unittest discover -s tests -v"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from dayluxe.agents import sales
from dayluxe.agents.email_agent import EmailAgent, classify
from dayluxe.agents.intake import IntakeError, intake_web_form
from dayluxe.agents.marketing import MarketingAgent
from dayluxe.agents.quote import QuoteAgent, build_quote
from dayluxe.approvals import ApprovalError, ApprovalManager
from dayluxe.ceo import CEO
from dayluxe.config import Settings, load_settings
from dayluxe.context import Context
from dayluxe.http_client import HttpClient, HttpError
from dayluxe.integrations.gmail import InboundEmail
from dayluxe.storage import Storage

TOKEN = "test-admin-token-1234567890"
SECRET = "test-webhook-secret"


class FakeNet:
    """Routes requests to handlers by (method, url-prefix). Records every call."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def on(self, method: str, prefix: str, handler: Any) -> None:
        # handler: (status, body_dict) or callable(url, body) -> (status, body_dict)
        self.routes.insert(0, (method, prefix, handler))

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        self.calls.append((method, url, headers, body))
        for m, prefix, handler in self.routes:
            if m == method and url.startswith(prefix):
                status, data = handler(url, body) if callable(handler) else handler
                return status, {}, json.dumps(data).encode()
        return 404, {}, b'{"error":{"message":"no fake route"}}'

    def count(self, method: str, prefix: str) -> int:
        return sum(1 for m, u, _, _ in self.calls if m == method and u.startswith(prefix))


def make_ctx(net: FakeNet | None = None, **overrides: Any) -> Context:
    s = Settings(admin_token=TOKEN, webhook_secret=SECRET, database_path=":memory:", business_phone="0402 749 443",
                 email_signature="Azher – Dayluxe Roofing", text_provider="none")
    for k, v in overrides.items():
        setattr(s, k, v)
    http = HttpClient(timeout=2, max_retries=2, transport=net or FakeNet(), sleep=lambda _: None)
    tmp = tempfile.mkdtemp()
    return Context.build(s, Storage(":memory:"), http, media_dir=os.path.join(tmp, "media"))


def gmail_net() -> FakeNet:
    net = FakeNet()
    net.on("POST", "https://oauth2.googleapis.com/token", (200, {"access_token": "at", "expires_in": 3600}))
    net.on("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile", (200, {"emailAddress": "owner@x.com"}))
    return net


def gmail_msg(mid: str, sender: str, subject: str, body: str) -> dict[str, Any]:
    return {"id": mid, "threadId": "t" + mid, "labelIds": ["INBOX", "UNREAD"], "payload": {
        "mimeType": "multipart/alternative",
        "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
                    {"name": "Message-ID", "value": f"<{mid}@mail>"}],
        "parts": [{"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}]}}


GMAIL_CREDS = dict(google_client_id="cid", google_client_secret="cs", google_refresh_token="rt")


# ---------------------------------------------------------------- unit tests
class ConfigTests(unittest.TestCase):
    def test_safe_defaults_and_redaction(self) -> None:
        for k in ("AUTO_SEND_EMAIL", "CEO_APPROVAL_REQUIRED", "OPENAI_API_KEY"):
            os.environ.pop(k, None)
        os.environ["OPENAI_API_KEY"] = "sk-secret"
        try:
            s = load_settings(None)
        finally:
            os.environ.pop("OPENAI_API_KEY")
        self.assertFalse(s.auto_send_email or s.auto_publish_social or s.auto_spend_ad_budget or s.auto_send_quotes)
        self.assertTrue(s.ceo_approval_required)
        self.assertEqual(s.redacted()["openai_api_key"], "set")
        self.assertNotIn("sk-secret", json.dumps(s.redacted()))

    def test_bad_bool_rejected(self) -> None:
        os.environ["AUTO_SEND_EMAIL"] = "maybe"
        try:
            with self.assertRaises(ValueError):
                load_settings(None)
        finally:
            os.environ.pop("AUTO_SEND_EMAIL")


class HttpTests(unittest.TestCase):
    def test_retries_transient_then_succeeds(self) -> None:
        net, n = FakeNet(), {"i": 0}

        def flaky(url: str, body: Any):
            n["i"] += 1
            return (503, {"error": "busy"}) if n["i"] < 3 else (200, {"ok": True})

        net.on("GET", "https://x.test", flaky)
        self.assertEqual(HttpClient(transport=net, max_retries=3, sleep=lambda _: None)
                         .request("GET", "https://x.test/a"), {"ok": True})
        self.assertEqual(n["i"], 3)

    def test_quota_not_retried(self) -> None:
        net = FakeNet()
        net.on("POST", "https://x.test", (429, {"error": {"message": "You exceeded your current quota",
                                                           "code": "insufficient_quota"}}))
        with self.assertRaises(HttpError) as cm:
            HttpClient(transport=net, max_retries=3, sleep=lambda _: None).request("POST", "https://x.test/i")
        self.assertEqual(cm.exception.kind, "quota")
        self.assertEqual(net.count("POST", "https://x.test"), 1)

    def test_auth_not_retried_and_secret_not_leaked(self) -> None:
        net = FakeNet()
        net.on("GET", "https://x.test", (401, {"error": {"message": "bad key"}}))
        with self.assertRaises(HttpError) as cm:
            HttpClient(transport=net, sleep=lambda _: None).request("GET", "https://x.test/a",
                                                                    params={"access_token": "SECRET"})
        self.assertEqual(cm.exception.kind, "auth")
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertEqual(len(net.calls), 1)

    def test_network_error_classified(self) -> None:
        def boom(*a: Any) -> Any:
            raise urllib.error.URLError("dns failure")

        with self.assertRaises(HttpError) as cm:
            HttpClient(transport=boom, max_retries=1, sleep=lambda _: None).request("GET", "https://x.test")
        self.assertEqual(cm.exception.kind, "network")


class SalesTests(unittest.TestCase):
    def test_urgent_leak(self) -> None:
        lead = sales.analyse("Hi, roof is leaking into the lounge in Parramatta, call 0412 345 678", name="Sam")
        self.assertEqual(lead["service"], "Roof repairs")
        self.assertEqual(lead["suburb"], "Parramatta")
        self.assertEqual(lead["urgency"], "urgent")
        self.assertTrue(lead["phone"].startswith("0412"))
        self.assertGreaterEqual(lead["score"], 80)
        self.assertIn("Parramatta", sales.owner_summary_ar(lead))

    def test_spam_and_year_not_postcode(self) -> None:
        spam = sales.analyse("We offer SEO services to rank your website, backlink packages https://a https://b")
        self.assertEqual(spam["status"], "spam")
        self.assertEqual(sales.detect_suburb("Booked for 2026, thanks"), "")
        self.assertEqual(sales.detect_suburb("Address: 5 Smith St, NSW 2150"), "NSW 2150")

    def test_fallback_reply_has_no_prices(self) -> None:
        ctx = make_ctx()
        text = sales.reply_fallback(ctx, sales.analyse("need gutter cleaning", name="Jo Smith"))
        self.assertIn("Hi Jo", text)
        self.assertNotIn("$", text)
        self.assertIn("Azher – Dayluxe Roofing", text)


class StorageTests(unittest.TestCase):
    def test_dedupe_by_email_and_phone(self) -> None:
        st = Storage(":memory:")
        a, new_a = st.upsert_lead({"name": "A", "email": "A@x.com", "phone": ""}, {"source": "web", "external_id": "1"})
        b, new_b = st.upsert_lead({"email": "a@x.com", "phone": "0400 000 000"}, {"source": "gmail", "external_id": "2"})
        c, new_c = st.upsert_lead({"phone": "+61 400 000 000"}, {"source": "web", "external_id": "3"})
        self.assertEqual((a, b, c), (a, a, a))
        self.assertEqual((new_a, new_b, new_c), (True, False, False))
        self.assertEqual(len(st.lead_enquiries(a)), 3)
        self.assertEqual(st.get_lead(a)["phone"], "0400000000")


# ------------------------------------------------------------ integration-ish
class IntakeAndApprovalTests(unittest.TestCase):
    def test_web_form_creates_lead_and_pending_reply(self) -> None:
        ctx = make_ctx()
        am = ApprovalManager(ctx)
        res = intake_web_form(ctx, am, {"your-name": "Ali", "your-email": "ali@example.com",
                                        "your-message": "Need roof restoration in Penrith please"})
        self.assertEqual(res["reply"], "approval_required")
        pend = ctx.storage.list_approvals("pending")
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["kind"], "email_reply")
        self.assertEqual(intake_web_form(ctx, am, {"your-name": "Ali", "your-email": "ali@example.com",
                                                   "your-message": "Need roof restoration in Penrith please"})["status"],
                         "duplicate")
        with self.assertRaises(IntakeError):
            intake_web_form(ctx, am, {"name": "x", "message": "hi"})

    def test_escalation_and_phone_only(self) -> None:
        ctx = make_ctx()
        am = ApprovalManager(ctx)
        r = intake_web_form(ctx, am, {"email": "c@x.com", "message": "I want a refund, your work is terrible"})
        self.assertIn("escalation", r)
        r2 = intake_web_form(ctx, am, {"phone": "0400111222", "service": "gutter cleaning"})
        self.assertEqual(r2["reply"], "owner_to_call")
        kinds = sorted(a["kind"] for a in ctx.storage.list_approvals("pending"))
        self.assertEqual(kinds, ["owner_attention", "owner_attention"])

    def test_approve_sends_once_and_records_approver(self) -> None:
        net = gmail_net()
        net.on("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send", (200, {"id": "sent1"}))
        ctx = make_ctx(net, **GMAIL_CREDS)
        am = ApprovalManager(ctx)
        intake_web_form(ctx, am, {"name": "Ali", "email": "ali@example.com", "message": "gutter guard quote Penrith"})
        aid = ctx.storage.list_approvals("pending")[0]["id"]
        self.assertEqual(net.count("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"), 0)
        res = am.decide(aid, True, "owner", {"body": "Edited reply"})
        self.assertEqual(res["status"], "executed")
        sent = [c for c in net.calls if c[1].endswith("/messages/send")]
        self.assertEqual(len(sent), 1)
        raw = json.loads(sent[0][3])["raw"]
        self.assertIn("Edited reply", base64.urlsafe_b64decode(raw + "==").decode())
        item = ctx.storage.get_approval(aid)
        self.assertEqual((item["status"], item["decided_by"]), ("executed", "owner"))
        self.assertEqual(ctx.storage.list_leads()[0]["status"], "contacted")
        with self.assertRaises(ApprovalError):
            am.decide(aid, True, "owner")

    def test_send_failure_reported_honestly(self) -> None:
        ctx = make_ctx()  # Gmail not configured
        am = ApprovalManager(ctx)
        intake_web_form(ctx, am, {"email": "z@x.com", "message": "roof painting quote"})
        aid = ctx.storage.list_approvals("pending")[0]["id"]
        res = am.decide(aid, True, "owner")
        self.assertEqual(res["status"], "failed")
        self.assertIn("Gmail is not connected", res["result"])

    def test_autopilot_requires_both_switches_except_explicit_social_opt_in(self) -> None:
        ctx = make_ctx(auto_send_email=True, auto_publish_social=True)  # CEO approval still required for email
        am = ApprovalManager(ctx)
        self.assertFalse(am.auto_allowed("email_reply", {}))
        self.assertTrue(am.auto_allowed("social_post", {}))
        self.assertFalse(am.auto_allowed("ad_campaign", {}))
        self.assertFalse(am.auto_allowed("outreach_email", {}))
        self.assertFalse(am.auto_allowed("seo_page_publish", {}))
        ctx2 = make_ctx(auto_send_email=True, ceo_approval_required=False)
        am2 = ApprovalManager(ctx2)
        self.assertTrue(am2.auto_allowed("email_reply", {}))
        self.assertFalse(am2.auto_allowed("social_post", {}))

    def test_ad_campaign_never_claims_live(self) -> None:
        ctx = make_ctx()
        am = ApprovalManager(ctx)
        MarketingAgent(ctx, am).run(today=date(2026, 10, 19))
        ad = [a for a in ctx.storage.list_approvals("pending") if a["kind"] == "ad_campaign"][0]
        res = am.decide(ad["id"], True, "owner")
        self.assertIn("NOT launched", res["result"])
        self.assertEqual(ctx.storage.get_approval(ad["id"])["status"], "approved")


class QuoteTests(unittest.TestCase):
    def _pricing(self, data: dict[str, Any]) -> str:
        f = Path(tempfile.mkdtemp()) / "pricing.json"
        f.write_text(json.dumps(data))
        return str(f)

    def test_incomplete_quote_blocked(self) -> None:
        ctx = make_ctx(pricing_file=self._pricing({"items": {"gutter_cleaning": {"label": "Gutter clean",
                                                                                 "unit": "lm", "unit_price": None}}}))
        lead_id, _ = ctx.storage.upsert_lead({"name": "Bo", "email": "bo@x.com"}, {"source": "web", "external_id": "q"})
        am = ApprovalManager(ctx)
        q = QuoteAgent(ctx, am).draft(lead_id, [{"item": "gutter_cleaning", "quantity": 40}])
        self.assertFalse(q["complete"])
        self.assertTrue(any("price" in m for m in q["missing"]))
        self.assertTrue(any("GST" in m for m in q["missing"]))
        with self.assertRaises(ApprovalError):
            am.decide(q["approval"]["id"], True, "owner")

    def test_complete_quote_maths(self) -> None:
        ctx = make_ctx(pricing_file=self._pricing({"gst_registered": True, "validity_days": 30, "items": {
            "gutter_guard": {"label": "Gutter guard", "unit": "lm", "unit_price": 12.5}}}))
        q = build_quote(ctx, {"name": "Bo Li", "email": "bo@x.com", "suburb": "Penrith"},
                        [{"item": "gutter_guard", "quantity": 40}], "Supply and fit gutter guard", "",
                        today=date(2026, 10, 18))
        self.assertTrue(q["complete"], q["missing"])
        self.assertIn("Subtotal (ex GST): $500.00", q["body"])
        self.assertIn("GST (10%): $50.00", q["body"])
        self.assertIn("Total (inc GST): $550.00", q["body"])
        self.assertIn("17 November 2026", q["body"])


class EmailAgentTests(unittest.TestCase):
    def test_classification(self) -> None:
        mk = lambda s, subj, b: InboundEmail("1", "t", "", s, s, subj, b, "")  # noqa: E731
        self.assertEqual(classify(mk("jobs@hipages.com.au", "New lead", "roof")), "platform_lead")
        self.assertEqual(classify(mk("a@b.com", "Quote", "need my gutters cleaned in Ryde")), "enquiry")
        self.assertEqual(classify(mk("a@b.com", "Re", "I'm not happy, still leaking after your job")), "escalation")
        self.assertEqual(classify(mk("acc@supplier.com", "Invoice 22", "please find invoice")), "supplier")
        self.assertEqual(classify(mk("noreply@bank.com", "Statement", "unsubscribe")), "notification")

    def test_gmail_cycle_and_no_reprocessing(self) -> None:
        net = gmail_net()
        msgs = {"m1": gmail_msg("m1", "Sam Lee <sam@ex.com>", "Roof leak", "Water coming in, Blacktown. 0411 222 333"),
                "m2": gmail_msg("m2", "hipages <jobs@hipages.com.au>", "New job", "Gutter cleaning in Penrith"),
                "m3": gmail_msg("m3", "x@spam.io", "Grow", "SEO services to rank your website + backlink deals")}
        net.on("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages?",
               (200, {"messages": [{"id": k} for k in msgs]}))
        for k, v in msgs.items():
            net.on("GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{k}", (200, v))
        net.on("POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts", (200, {"id": "d1"}))
        ctx = make_ctx(net, **GMAIL_CREDS)
        agent = EmailAgent(ctx, ApprovalManager(ctx))
        s1 = agent.run()
        self.assertEqual((s1["new_leads"], s1["drafts"], s1["ignored"]), (2, 1, 1))
        kinds = sorted(a["kind"] for a in ctx.storage.list_approvals("pending"))
        self.assertEqual(kinds, ["email_reply", "owner_attention"])
        reply = [a for a in ctx.storage.list_approvals("pending") if a["kind"] == "email_reply"][0]
        self.assertEqual(reply["payload"]["draft_id"], "d1")
        self.assertEqual(reply["payload"]["to"], "sam@ex.com")
        s2 = agent.run()
        self.assertEqual((s2["new_leads"], s2["drafts"]), (0, 0))
        self.assertEqual(len(ctx.storage.list_approvals("pending")), 2)


class ProviderFallbackTests(unittest.TestCase):
    def test_llm_quota_falls_back_and_stops_calling(self) -> None:
        net = FakeNet()
        net.on("POST", "https://api.openai.com/v1/chat/completions",
               (429, {"error": {"message": "insufficient_quota"}}))
        ctx = make_ctx(net, text_provider="auto", openai_api_key="sk")
        r1 = ctx.llm.generate("p", "FALLBACK")
        r2 = ctx.llm.generate("p", "FALLBACK")
        self.assertEqual((r1.text, r1.provider, r2.text), ("FALLBACK", "template", "FALLBACK"))
        self.assertEqual(net.count("POST", "https://api.openai.com/v1/chat/completions"), 1)

    def test_gemini_success(self) -> None:
        net = FakeNet()
        net.on("POST", "https://generativelanguage.googleapis.com/",
               (200, {"candidates": [{"content": {"parts": [{"text": "Hello from Gemini"}]}}]}))
        ctx = make_ctx(net, text_provider="gemini", gemini_api_key="g")
        r = ctx.llm.generate("p", "F")
        self.assertEqual((r.text, r.provider), ("Hello from Gemini", "gemini"))
        self.assertEqual(net.calls[0][2].get("x-goog-api-key"), "g")
        self.assertNotIn("key=", net.calls[0][1])

    def test_image_quota_disables_without_retry(self) -> None:
        net = FakeNet()
        net.on("POST", "https://api.openai.com/v1/images", (402, {"error": {"message": "billing hard limit"}}))
        ctx = make_ctx(net, openai_api_key="sk", generate_images=True)
        f1, e1 = ctx.images.generate("roof")
        f2, e2 = ctx.images.generate("roof")
        self.assertEqual((f1, f2), ("", ""))
        self.assertEqual(net.count("POST", "https://api.openai.com/v1/images"), 1)
        self.assertIn("unavailable", e2)

    def test_image_success_saved(self) -> None:
        net = FakeNet()
        png = base64.b64encode(b"\x89PNG fake").decode()
        net.on("POST", "https://api.openai.com/v1/images", (200, {"data": [{"b64_json": png}]}))
        ctx = make_ctx(net, openai_api_key="sk", generate_images=True, public_base_url="https://app.example")
        name, err = ctx.images.generate("roof")
        self.assertEqual(err, "")
        self.assertTrue((ctx.images.media_dir / name).is_file())
        self.assertEqual(ctx.images.public_url(name), f"https://app.example/media/{name}")


class CEOTests(unittest.TestCase):
    def test_cycle_with_nothing_connected(self) -> None:
        ctx = make_ctx()
        report = CEO(ctx).run_cycle("test")
        self.assertEqual(report["status"], "ok", report["steps"])
        self.assertEqual(report["steps"]["email"]["status"], "skipped")
        self.assertGreaterEqual(report["steps"]["marketing"]["result"]["planned"], 1)
        self.assertTrue(report["steps"]["seo"]["result"]["drafted"].startswith("Roof Restoration"))
        again = CEO(ctx).run_cycle("test")
        self.assertEqual(again["steps"]["marketing"]["result"]["planned"], 0)  # no duplicate posts

    def test_one_failing_integration_does_not_stop_others(self) -> None:
        net = gmail_net()
        net.on("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages?", (500, {"error": "down"}))
        ctx = make_ctx(net, **GMAIL_CREDS)
        report = CEO(ctx).run_cycle("test")
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["steps"]["email"]["status"], "failed")
        self.assertEqual(report["steps"]["marketing"]["status"], "completed")
        self.assertEqual(report["steps"]["seo"]["status"], "completed")

    def test_wordpress_draft_then_publish_on_approval(self) -> None:
        net = FakeNet()
        wp = "https://site.test/wp-json/wp/v2/"
        net.on("GET", wp + "users/me", (200, {"name": "Azher", "capabilities": {"edit_pages": True}}))
        net.on("GET", wp + "pages", (200, [{"id": 1, "slug": "roofing-parramatta", "status": "publish",
                                           "title": {"raw": "Roofing Parramatta"}, "content": {"raw": "short"}}]))
        net.on("POST", wp + "pages", (201, {"id": 77}))
        net.on("POST", wp + "pages/77", (200, {"id": 77, "status": "publish", "link": "https://site.test/p"}))
        ctx = make_ctx(net, wp_base_url="https://site.test", wp_username="u", wp_app_password="p")
        ceo = CEO(ctx)
        report = ceo.run_cycle("test")
        self.assertEqual(report["steps"]["seo"]["result"]["drafted"], "Roof Restoration & Repairs in Penrith")
        self.assertTrue(report["steps"]["seo"]["result"]["issues"])
        pub = [a for a in ctx.storage.list_approvals("pending") if a["kind"] == "seo_page_publish"][0]
        self.assertEqual(net.count("POST", wp + "pages/77"), 0)  # not published without approval
        res = ceo.approvals.decide(pub["id"], True, "owner")
        self.assertEqual(res["status"], "executed")
        self.assertEqual(net.count("POST", wp + "pages/77"), 1)

    def test_followup_drafted_for_stale_contacted_lead(self) -> None:
        ctx = make_ctx()
        lead_id, _ = ctx.storage.upsert_lead({"name": "Kim", "email": "kim@x.com", "service": "Gutters"},
                                             {"source": "web", "external_id": "f"})
        ctx.storage.update_lead_status(lead_id, "contacted")
        with ctx.storage.tx() as c:
            c.execute("UPDATE leads SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (lead_id,))
        ceo = CEO(ctx)
        self.assertEqual(ceo.followups()["drafted"], 1)
        self.assertEqual(ceo.followups()["drafted"], 0)


# ------------------------------------------------------------------- web
class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from dayluxe.web import App, make_server

        cls.ctx = make_ctx()
        cls.app = App(CEO(cls.ctx), media_dir=str(cls.ctx.images.media_dir))
        cls.srv = make_server(cls.app, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method: str, path: str, data: Any = None, headers: dict[str, str] | None = None,
            form: bool = False) -> tuple[int, str, dict[str, str]]:
        body = None
        hdrs = dict(headers or {})
        if data is not None:
            if form:
                body = urllib.parse.urlencode(data).encode()
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                body = json.dumps(data).encode()
                hdrs["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=body, headers=hdrs, method=method)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a: Any, **k: Any) -> None:
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def test_flow(self) -> None:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        self.assertEqual(self.req("GET", "/health")[0], 200)
        self.assertEqual(self.req("GET", "/api/status")[0], 401)
        self.assertEqual(self.req("GET", "/")[0], 303)
        self.assertEqual(self.req("POST", "/webhook/lead", {"email": "a@b.com", "message": "roof"})[0], 401)
        code, body, _ = self.req("POST", "/webhook/lead",
                                 {"name": "Lee", "email": "lee@ex.com", "message": "roof restoration Blacktown"},
                                 {"X-Webhook-Secret": SECRET})
        self.assertEqual(code, 200, body)
        self.assertTrue(json.loads(body)["new_lead"])
        code, body, _ = self.req("POST", "/webhook/lead", {"name": "x"}, {"X-Webhook-Secret": SECRET})
        self.assertEqual(code, 422)
        code, body, _ = self.req("GET", "/api/status", headers=auth)
        self.assertEqual(code, 200)
        status = json.loads(body)
        self.assertEqual(status["counts"]["leads_new"], 1)
        self.assertNotIn(TOKEN, body)
        aid = status["pending_approvals"][0]["id"]
        code, body, _ = self.req("POST", f"/api/approvals/{aid}/reject", {}, auth)
        self.assertEqual((code, json.loads(body)["status"]), (200, "rejected"))
        code, body, _ = self.req("POST", f"/api/approvals/{aid}/approve", {}, auth)
        self.assertEqual(code, 409)
        # cookie login + dashboard render
        code, _, hdrs = self.req("POST", "/login", {"token": TOKEN}, form=True)
        self.assertEqual(code, 303)
        cookie = hdrs["Set-Cookie"].split(";")[0]
        code, page, _ = self.req("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        self.assertIn("Dayluxe AI CEO", page)
        self.assertIn("System health", page)
        code, _, _ = self.req("POST", "/ui/run", {}, {"Cookie": cookie, "Origin": "https://evil.example"}, form=True)
        self.assertEqual(code, 403)
        code, _, hdrs = self.req("POST", "/ui/run", {}, {"Cookie": cookie}, form=True)
        self.assertEqual(code, 303)
        self.app._bg.join(timeout=20)
        self.assertEqual(self.ctx.storage.last_run()["status"], "ok")
        self.assertEqual(self.req("GET", "/media/../../etc/passwd")[0], 404)


if __name__ == "__main__":
    unittest.main()
