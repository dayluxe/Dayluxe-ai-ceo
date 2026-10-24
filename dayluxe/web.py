"""HTTP server: owner dashboard, JSON API, website-form webhook. Standard library only."""
from __future__ import annotations

import hmac
import html
import json
import logging
import re
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .agents.intake import IntakeError, intake_web_form
from .agents.quote import QuoteAgent
from .approvals import KINDS, ApprovalError
from .ceo import CEO

log = logging.getLogger(__name__)

MAX_BODY = 45_000_000
COOKIE = "dlx_session"
_MEDIA_RE = re.compile(r"^[a-f0-9]{24}\.(?:png|jpg|jpeg|webp|mp4|mov)$")
_ROUTE_ID = re.compile(r"^/(?:api|ui)/approvals/(\d+)(?:/(approve|reject))?$")


def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x))


class App:
    """Holds the CEO and helpers; one instance shared by all request threads."""

    def __init__(self, ceo: CEO, media_dir: str = "data/media") -> None:
        self.ceo = ceo
        self.ctx = ceo.ctx
        self.quotes = QuoteAgent(ceo.ctx, ceo.approvals)
        self.media_dir = Path(media_dir)
        self._bg: threading.Thread | None = None

    def check_token(self, token: str) -> bool:
        expected = self.ctx.settings.admin_token
        return bool(expected) and len(expected) >= 16 and hmac.compare_digest(token.encode(), expected.encode())

    def start_run_async(self, trigger: str) -> bool:
        if self._bg and self._bg.is_alive():
            return False
        self._bg = threading.Thread(target=self.ceo.run_cycle, args=(trigger,), daemon=True)
        self._bg.start()
        return True


class Handler(BaseHTTPRequestHandler):
    app: App  # injected by make_server
    server_version = "DayluxeAICEO/1.0"

    # ------------------------------------------------------------ helpers
    def log_message(self, fmt: str, *args: Any) -> None:  # route access logs through logging, path only
        log.info("%s %s", self.command, urllib.parse.urlsplit(self.path).path)

    def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, default=str, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def html_page(self, status: int, body: str) -> None:
        self._send(status, body.encode(), "text/html; charset=utf-8",
                   {"Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:"})

    def redirect(self, location: str, extra: dict[str, str] | None = None) -> None:
        hdrs = {"Location": location}
        hdrs.update(extra or {})
        self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", hdrs)

    def read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not raw:
            return {}
        if ctype == "application/json":
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data
        parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}

    def token_from_request(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[COOKIE].value if COOKIE in cookie else ""

    def authed(self) -> bool:
        return self.app.check_token(self.token_from_request())

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urllib.parse.urlsplit(origin).netloc == self.headers.get("Host", "")

    # ------------------------------------------------------------ routing
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._get()
        except Exception:  # noqa: BLE001
            log.exception("GET failed")
            self.json(500, {"error": "internal error"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._post()
        except (ValueError, json.JSONDecodeError) as exc:
            self.json(400, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            log.exception("POST failed")
            self.json(500, {"error": "internal error"})

    def _get(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        q = urllib.parse.parse_qs(url.query)
        if path == "/health":
            return self.json(200, {"status": "ok", "storage": self.app.ctx.storage.healthy()})
        if path.startswith("/media/"):
            name = path.rsplit("/", 1)[-1]
            f = self.app.media_dir / name
            if _MEDIA_RE.match(name) and f.is_file():
                suffix = f.suffix.lower()
                mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".webp": "image/webp", ".mp4": "video/mp4", ".mov": "video/quicktime"}.get(suffix, "application/octet-stream")
                return self._send(200, f.read_bytes(), mime, {"Cache-Control": "public, max-age=86400"})
            return self.json(404, {"error": "not found"})
        if path == "/login":
            return self.html_page(200, login_page())
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
        if not self.authed():
            if path.startswith("/api/"):
                return self.json(401, {"error": "unauthorized"})
            return self.redirect("/login")

        st = self.app.ctx.storage
        if path == "/":
            return self.html_page(200, dashboard_page(self.app))
        if path == "/api/status":
            return self.json(200, {"health": self.app.ceo.health(max_age=60), **self.app.ceo.dashboard_data(),
                                   "last_run": st.last_run(), "settings": self.app.ctx.settings.redacted()})
        if path == "/api/leads":
            status = q.get("status", [None])[0]
            return self.json(200, st.list_leads(status))
        m = re.match(r"^/api/leads/(\d+)$", path)
        if m:
            lead = st.get_lead(int(m.group(1)))
            if not lead:
                return self.json(404, {"error": "not found"})
            return self.json(200, {**lead, "enquiries": st.lead_enquiries(lead["id"])})
        if path == "/api/approvals":
            status = q.get("status", ["pending"])[0] or None
            return self.json(200, st.list_approvals(None if status == "all" else status))
        m = _ROUTE_ID.match(path)
        if m and not m.group(2):
            item = st.get_approval(int(m.group(1)))
            return self.json(200, item) if item else self.json(404, {"error": "not found"})
        if path == "/api/content":
            return self.json(200, st.list_content(100))
        if path == "/api/prospects":
            return self.json(200, st.list_prospects())
        if path == "/api/runs/last":
            return self.json(200, st.last_run() or {})
        return self.json(404, {"error": "not found"})

    def _post(self) -> None:
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        app = self.app


        if path == "/api/media/upload":
            data = self.read_body()
            filename = str(data.get("filename", ""))
            mime = str(data.get("mime", "")).lower()
            encoded = str(data.get("data", ""))
            allowed = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                       ".webp": "image/webp", ".mp4": "video/mp4", ".mov": "video/quicktime"}
            suffix = Path(filename).suffix.lower()
            if suffix not in allowed or mime not in {allowed[suffix], "application/octet-stream"}:
                return self.json(415, {"error": "Use PNG/JPG/WEBP images or MP4/MOV videos."})
            try:
                raw = __import__("base64").b64decode(encoded, validate=True)
            except Exception:
                return self.json(400, {"error": "Invalid file data"})
            if not raw or len(raw) > 30_000_000:
                return self.json(413, {"error": "Each file must be smaller than 30 MB."})
            digest = __import__("hashlib").sha256(raw).hexdigest()[:24]
            stored_name = digest + suffix
            app.media_dir.mkdir(parents=True, exist_ok=True)
            (app.media_dir / stored_name).write_bytes(raw)
            public_url = app.ctx.settings.public_base_url.rstrip("/") + "/media/" + stored_name if app.ctx.settings.public_base_url else ""
            return self.json(201, {"ok": True, "filename": stored_name, "url": public_url,
                                   "type": "video" if suffix in (".mp4", ".mov") else "image", "bytes": len(raw)})

        if path == "/api/media/publish-best":
            data = self.read_body()
            caption = str(data.get("caption", "")).strip()
            if not caption:
                return self.json(400, {"error": "Write or dictate a caption first."})
            if not app.ctx.meta.configured:
                return self.json(503, {"error": "Meta is not connected yet. Upload works, but publishing needs Facebook Page credentials."})
            files = [p for p in app.media_dir.iterdir() if p.is_file() and _MEDIA_RE.match(p.name)]
            if not files:
                return self.json(400, {"error": "Upload at least one image or video first."})
            videos = [p for p in files if p.suffix.lower() in (".mp4", ".mov")]
            candidates = videos or [p for p in files if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
            if not candidates:
                return self.json(400, {"error": "No supported media found."})
            best = max(candidates, key=lambda p: (p.stat().st_size, p.stat().st_mtime))
            if not app.ctx.settings.public_base_url:
                return self.json(503, {"error": "Set PUBLIC_BASE_URL in Render before publishing media."})
            media_url = app.ctx.settings.public_base_url.rstrip("/") + "/media/" + best.name
            if best.suffix.lower() in (".mp4", ".mov"):
                post_id = app.ctx.meta.publish_facebook_video(caption, media_url)
                platform = "Facebook video"
            else:
                post_id = app.ctx.meta.publish_facebook(caption, media_url)
                platform = "Facebook photo"
            app.ctx.storage.log_action("media", "publish selected media", "completed", f"{platform}; asset={best.name}; post={post_id}")
            return self.json(200, {"ok": True, "platform": platform, "filename": best.name, "post_id": post_id})

        if path == "/webhook/lead":
            secret = self.headers.get("X-Webhook-Secret", "")
            expected = app.ctx.settings.webhook_secret
            if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
                return self.json(401, {"error": "bad webhook secret"})
            try:
                result = intake_web_form(app.ctx, app.ceo.approvals, self.read_body())
            except IntakeError as exc:
                return self.json(422, {"error": str(exc)})
            return self.json(200, {k: v for k, v in result.items() if k != "summary_ar"})

        if path == "/login":
            data = self.read_body()
            token = str(data.get("token", ""))
            if app.check_token(token):
                cookie = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"
                if self.headers.get("X-Forwarded-Proto", "") == "https":
                    cookie += "; Secure"
                return self.redirect("/", {"Set-Cookie": cookie})
            time.sleep(1)  # slow down guessing
            return self.html_page(401, login_page("Wrong token"))

        if not self.authed():
            return self.json(401, {"error": "unauthorized"})
        if not self.same_origin():
            return self.json(403, {"error": "cross-origin request blocked"})
        who = "owner"

        if path in ("/api/run", "/ui/run"):
            started = app.start_run_async("manual")
            if path.startswith("/ui"):
                return self.redirect("/?msg=" + urllib.parse.quote("Run started" if started else "Run already in progress"))
            return self.json(202 if started else 409, {"started": started})

        m = _ROUTE_ID.match(path)
        if m and m.group(2):
            approval_id, decision = int(m.group(1)), m.group(2)
            data = self.read_body()
            edits = data.get("edits") if isinstance(data.get("edits"), dict) else {
                k: data[k] for k in ("subject", "body", "message") if k in data}
            try:
                res = app.ceo.approvals.decide(approval_id, decision == "approve", who, edits)
            except ApprovalError as exc:
                if path.startswith("/ui"):
                    return self.redirect("/?msg=" + urllib.parse.quote(f"#{approval_id}: {exc}"))
                return self.json(409, {"error": str(exc)})
            if path.startswith("/ui"):
                msg = f"#{approval_id} {res['status']}" + (f": {res.get('result', '')}" if res.get("result") else "")
                return self.redirect("/?msg=" + urllib.parse.quote(msg[:300]))
            return self.json(200, res)

        m = re.match(r"^/api/leads/(\d+)/status$", path)
        if m:
            status = str(self.read_body().get("status", ""))
            if not app.ctx.storage.update_lead_status(int(m.group(1)), status):
                return self.json(404, {"error": "not found"})
            return self.json(200, {"ok": True})

        if path == "/api/quotes":
            d = self.read_body()
            lines = d.get("lines") or []
            if not isinstance(lines, list):
                raise ValueError("lines must be a list")
            res = app.quotes.draft(int(d.get("lead_id", 0)), lines, str(d.get("scope_notes", "")),
                                   str(d.get("exclusions", "")), int(d.get("revision", 1)))
            return self.json(200, res)

        if path == "/api/prospects":
            pid, created = app.ctx.storage.add_prospect(self.read_body())
            return self.json(201 if created else 200, {"id": pid, "created": created})

        return self.json(404, {"error": "not found"})


# ------------------------------------------------------------------ pages
STYLE = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d2321;--mute:#5d6662;--line:#e2dfd8;--brand:#1f5f4a;
--ok:#1f7a4a;--warn:#a96a00;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#121514;--card:#1b201e;--ink:#e8ece9;--mute:#9aa49f;
--line:#2c3330;--brand:#5fbf98;--ok:#5fbf98;--warn:#e0a640;--bad:#ef6a60}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,
Segoe UI,Roboto,sans-serif}main{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
h1{font-size:20px;margin:8px 0}h2{font-size:16px;margin:0 0 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
.kpi b{display:block;font-size:24px}.kpi span{color:var(--mute);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:6px;border-top:1px solid
var(--line);vertical-align:top}th{color:var(--mute);font-weight:600}
.s-ok,.s-completed,.s-executed{color:var(--ok)}.s-error,.s-failed{color:var(--bad)}
.s-not_configured,.s-pending,.s-skipped,.s-approval_required{color:var(--warn)}
.ar{direction:rtl;text-align:right;font-family:Tahoma,system-ui,sans-serif}
button,input[type=submit]{background:var(--brand);color:#fff;border:0;border-radius:8px;padding:8px 14px;
font:inherit;cursor:pointer}button.secondary{background:transparent;color:var(--bad);border:1px solid var(--bad)}
textarea,input[type=text],input[type=password]{width:100%;background:var(--bg);color:var(--ink);border:1px solid
var(--line);border-radius:8px;padding:8px;font:inherit}textarea{min-height:140px}
details{margin-top:6px}.msg{background:var(--card);border-left:4px solid var(--brand);padding:8px 12px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}.mute{color:var(--mute)}pre{white-space:pre-wrap;margin:0}
.wrap{overflow-x:auto}
"""


def layout(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{esc(title)}</title><style>{STYLE}</style>"
            f"</head><body><main>{body}</main></body></html>")


def login_page(error: str = "") -> str:
    err = f"<p class='s-error'>{esc(error)}</p>" if error else ""
    return layout("Dayluxe AI CEO — Login", f"""<div class='card' style='max-width:420px;margin:60px auto'>
<h1>Dayluxe AI CEO</h1>{err}<form method='post' action='/login'>
<label>Admin token<input type='password' name='token' autocomplete='current-password' required></label>
<div class='row'><input type='submit' value='Sign in'></div></form></div>""")


def dashboard_page(app: App) -> str:
    st = app.ctx.storage
    ceo = app.ceo
    health = ceo.health(max_age=120)
    data = ceo.dashboard_data()
    last = st.last_run()
    c = data["counts"]
    problems = app.ctx.settings.validate()

    kpis = "".join(f"<div class='card kpi'><b>{esc(v)}</b><span>{esc(k)}</span></div>" for k, v in [
        ("New leads", c["leads_new"]), ("Awaiting approval", c["approvals_pending"]),
        ("Quotes pending", c["quotes_pending"]), ("Emails pending", c["emails_pending"]),
        ("Failed (24h)", c["actions_failed_24h"])])
    health_rows = "".join(f"<tr><td>{esc(k)}</td><td class='s-{esc(v['status'])}'>{esc(v['status'])}</td>"
                          f"<td>{esc(v['detail'])}</td></tr>" for k, v in health.items())
    warn = "".join(f"<li>{esc(p)}</li>" for p in problems)
    priorities = "".join(f"<li class='ar'>{esc(p['summary_ar'])} — <span dir='ltr'>{esc(p['next_action'])}</span></li>"
                         for p in data["priorities"]) or "<li class='mute'>No new leads.</li>"
    steps = "".join(f"<li>{esc(s)}</li>" for s in data["next_steps"])

    approvals_html = []
    for a in st.list_approvals("pending", 50):
        p = a["payload"]
        editable = a["kind"] in ("email_reply", "followup", "quote", "outreach_email", "social_post")
        text_field = "message" if a["kind"] == "social_post" else "body"
        summary_ar = p.get("summary_ar") or p.get("note_ar") or ""
        details = ""
        if a["kind"] in ("email_reply", "followup", "quote", "outreach_email"):
            details = f"<p class='mute'>To: {esc(p.get('to'))} — Subject: {esc(p.get('subject'))}</p>"
        if p.get("missing"):
            details += "<p class='s-error'>Missing: " + esc("; ".join(p["missing"])) + "</p>"
        if editable:
            body = (f"<textarea name='{text_field}'>{esc(p.get(text_field, ''))}</textarea>")
        else:
            body = f"<pre>{esc(p.get('plan') or json.dumps(p, ensure_ascii=False, indent=1))}</pre>"
        approvals_html.append(f"""<div class='card'><b>#{a['id']} · {esc(KINDS.get(a['kind'], a['kind']))}</b>
<div>{esc(a['title'])}</div>{f"<p class='ar'>{esc(summary_ar)}</p>" if summary_ar else ''}{details}
<form method='post' action='/ui/approvals/{a['id']}/approve'>{body}
<div class='row'><input type='submit' value='Approve{' & send' if a['kind'] in ('email_reply','followup','quote','outreach_email') else ''}'>
<button class='secondary' formaction='/ui/approvals/{a['id']}/reject'>Reject</button></div></form></div>""")

    lead_rows = "".join(
        f"<tr><td>{l['id']}</td><td>{esc(l['name'])}<br><span class='mute'>{esc(l['email'])} {esc(l['phone'])}</span></td>"
        f"<td>{esc(l['service'])}</td><td>{esc(l['suburb'])}</td><td>{l['score']}</td>"
        f"<td class='s-{'error' if l['urgency']=='urgent' else 'ok'}'>{esc(l['urgency'])}</td><td>{esc(l['status'])}</td></tr>"
        for l in st.list_leads(None, 30))
    content_rows = "".join(
        f"<tr><td>{esc(x['kind'])}</td><td>{esc(x['title'])}<details><summary>view</summary><pre>{esc(x['body'])}</pre>"
        f"</details></td><td class='s-{esc(x['status'])}'>{esc(x['status'])}</td></tr>" for x in st.list_content(20))
    action_rows = "".join(
        f"<tr><td class='mute'>{esc(a['created_at'][:16].replace('T', ' '))}</td><td>{esc(a['agent'])}</td>"
        f"<td>{esc(a['action'])}</td><td class='s-{esc(a['status'])}'>{esc(a['status'])}</td>"
        f"<td class='mute'>{esc(a['detail'][:160])}</td></tr>" for a in data["recent_actions"][:25])
    last_txt = (f"Last run #{last['id']} ({esc(last['trigger'])}): <b class='s-{esc(last['status'])}'>"
                f"{esc(last['status'])}</b> at {esc(last['finished_at'] or last['started_at'])}") if last else "No runs yet."

    media_ui = "<div class='card' id='media-studio'><h2>Media studio — الصور والفيديو والصوت</h2>\n<p class='mute'>ارفع صور وفيديوهات شغل Dayluxe. الاختيار الحالي تقني حسب نوع الملف وحجمه وحداثته، وليس تحليلاً بصرياً كاملاً.</p>\n<label>اختيار صور أو فيديوهات<input id='media-files' type='file' accept='image/png,image/jpeg,image/webp,video/mp4,video/quicktime' multiple></label>\n<div class='row'><button type='button' id='upload-media'>رفع الملفات</button></div><p id='upload-status' class='mute' aria-live='polite'></p>\n<label>نص المنشور<textarea id='media-caption' placeholder='اكتب نص المنشور أو استخدم الميكروفون...'></textarea></label>\n<div class='row'><button type='button' id='voice-caption'>🎙️ احچي المنشور</button><button type='button' id='publish-best'>اختيار الأفضل ونشره على Facebook</button></div>\n<p class='mute' id='voice-status'>الميكروفون يحتاج إذن المتصفح ويدعم Chrome غالباً.</p></div>\n<script>\n(function(){\n const files=document.getElementById('media-files'), status=document.getElementById('upload-status');\n document.getElementById('upload-media').addEventListener('click',async()=>{\n  if(!files.files.length){status.textContent='اختار صورة أو فيديو أولاً.';return;}\n  for(const file of files.files){\n   if(file.size>30000000){status.textContent=file.name+': أكبر من 30 MB.';continue;}\n   status.textContent='جاري رفع '+file.name+'...';\n   const b64=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});\n   try{const res=await fetch('/api/media/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:file.name,mime:file.type||'application/octet-stream',data:b64})});const d=await res.json();if(!res.ok)throw new Error(d.error||'فشل الرفع');status.textContent='تم رفع '+file.name+' بنجاح.';}\n   catch(e){status.textContent='تعذر رفع '+file.name+': '+e.message;}\n  }\n });\n document.getElementById('voice-caption').addEventListener('click',()=>{\n  const SR=window.SpeechRecognition||window.webkitSpeechRecognition, msg=document.getElementById('voice-status');\n  if(!SR){msg.textContent='المتصفح لا يدعم الصوت إلى نص؛ جرّب Chrome على Android.';return;}\n  const rec=new SR();rec.lang='ar-IQ';rec.interimResults=false;rec.maxAlternatives=1;msg.textContent='احچي هسه...';\n  rec.onresult=e=>{const box=document.getElementById('media-caption');box.value+=(box.value?' ':'')+e.results[0][0].transcript;msg.textContent='تم تحويل كلامك إلى نص.';};\n  rec.onerror=e=>msg.textContent='تعذر استخدام الميكروفون: '+e.error;rec.start();\n });\n document.getElementById('publish-best').addEventListener('click',async()=>{\n  const msg=document.getElementById('voice-status'), caption=document.getElementById('media-caption').value.trim();\n  if(!caption){msg.textContent='اكتب أو املي نص المنشور أولاً.';return;}msg.textContent='جاري اختيار الملف ومحاولة النشر...';\n  try{const res=await fetch('/api/media/publish-best',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({caption})});const d=await res.json();if(!res.ok)throw new Error(d.error||'فشل النشر');msg.textContent='تم النشر على '+d.platform+'.';}\n  catch(e){msg.textContent=e.message;}\n });\n})();\n</script>"

    body = f"""<header><h1>Dayluxe AI CEO</h1><div class='row'><form method='post' action='/ui/run'>
<input type='submit' value='Run cycle now'></form><a href='/logout' class='mute'>Log out</a></div></header>
{{MSG}}<p class='mute'>{last_txt}</p>
{f"<div class='card'><b>Configuration warnings</b><ul>{warn}</ul></div>" if warn else ''}
<div class='grid'>{kpis}</div>
<div class='card'><h2>Today's priorities</h2><ul>{priorities}</ul><h2>Next steps</h2><ul>{steps}</ul></div>

{media_ui}

<h2>Waiting for your approval</h2>{''.join(approvals_html) or "<p class='mute'>Nothing waiting.</p>"}
<div class='card wrap'><h2>System health</h2><table><tr><th>Service</th><th>Status</th><th>Detail</th></tr>{health_rows}</table></div>
<div class='card wrap'><h2>Leads</h2><table><tr><th>#</th><th>Customer</th><th>Service</th><th>Suburb</th><th>Score</th>
<th>Urgency</th><th>Status</th></tr>{lead_rows}</table></div>
<div class='card wrap'><h2>Marketing & website content</h2><table><tr><th>Type</th><th>Title</th><th>Status</th></tr>{content_rows}</table></div>
<div class='card wrap'><h2>Recent actions</h2><table><tr><th>Time (UTC)</th><th>Agent</th><th>Action</th><th>Status</th>
<th>Detail</th></tr>{action_rows}</table></div>"""
    return layout("Dayluxe AI CEO", body)


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})

    # Inject the flash message without a template engine.
    orig_html = handler.html_page

    def html_page(self: Handler, status: int, body: str) -> None:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        msg = qs.get("msg", [""])[0]
        body = body.replace("{MSG}", f"<p class='msg'>{esc(msg)}</p>" if msg else "")
        orig_html(self, status, body)

    handler.html_page = html_page  # type: ignore[method-assign]
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def start_scheduler(app: App, interval_minutes: int, stop: threading.Event) -> threading.Thread | None:
    if interval_minutes <= 0:
        return None

    def loop() -> None:
        while not stop.wait(interval_minutes * 60):
            try:
                app.ceo.run_cycle("scheduled")
            except Exception:  # noqa: BLE001
                log.exception("scheduled run failed")

    t = threading.Thread(target=loop, name="scheduler", daemon=True)
    t.start()
    return t
