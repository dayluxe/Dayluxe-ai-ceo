"""SQLite persistence: CRM (leads + enquiry history), approvals, action log, content, runs.

SQLite is part of Python's standard library, so persistence works with no extra
service. On Render, mount a persistent disk and point DATABASE_PATH at it,
otherwise data is lost on every deploy (the dashboard warns about this).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    suburb TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL DEFAULT 'normal',
    score INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    next_action TEXT NOT NULL DEFAULT '',
    first_source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);

CREATE TABLE IF NOT EXISTS enquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    idempotency_key TEXT NOT NULL UNIQUE,
    lead_id INTEGER,
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    scheduled_for TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    dedupe_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organisation TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    consent_basis TEXT NOT NULL DEFAULT '',
    source_note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    UNIQUE(organisation, email)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    report TEXT NOT NULL DEFAULT ''
);
"""

ACTION_STATUSES = {"completed", "pending", "failed", "approval_required", "skipped"}
APPROVAL_STATUSES = {"pending", "approved", "rejected", "executed", "failed"}
LEAD_STATUSES = {"new", "contacted", "quoted", "won", "lost", "spam"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalise_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("61") and len(digits) == 11:
        digits = "0" + digits[2:]
    return digits


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.persistent = path != ":memory:"
        if self.persistent:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.persistent:
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def healthy(self) -> bool:
        try:
            with self.tx() as c:
                c.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ----------------------------------------------------------------- leads
    def enquiry_exists(self, source: str, external_id: str) -> bool:
        with self.tx() as c:
            row = c.execute(
                "SELECT 1 FROM enquiries WHERE source=? AND external_id=?", (source, external_id)
            ).fetchone()
        return row is not None

    def upsert_lead(self, lead: dict[str, Any], enquiry: dict[str, Any]) -> tuple[int, bool]:
        """Insert or merge a lead. Returns (lead_id, created_new_lead).

        Duplicates are matched by email or normalised phone. A repeat enquiry with
        the same (source, external_id) is ignored by the caller via enquiry_exists.
        """
        email = (lead.get("email") or "").strip().lower()
        phone = normalise_phone(lead.get("phone") or "")
        ts = now_iso()
        with self.tx() as c:
            row = None
            if email:
                row = c.execute("SELECT * FROM leads WHERE email=? ORDER BY id LIMIT 1", (email,)).fetchone()
            if row is None and phone:
                row = c.execute("SELECT * FROM leads WHERE phone=? ORDER BY id LIMIT 1", (phone,)).fetchone()
            created = row is None
            if created:
                cur = c.execute(
                    """INSERT INTO leads (name,email,phone,suburb,service,urgency,score,status,next_action,
                       first_source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        lead.get("name", ""), email, phone, lead.get("suburb", ""), lead.get("service", ""),
                        lead.get("urgency", "normal"), int(lead.get("score", 0)), lead.get("status", "new"),
                        lead.get("next_action", ""), enquiry.get("source", ""), ts, ts,
                    ),
                )
                lead_id = int(cur.lastrowid)
            else:
                lead_id = int(row["id"])
                # Fill blanks only — never overwrite owner-verified data with guesses.
                merged = {
                    "name": row["name"] or lead.get("name", ""),
                    "email": row["email"] or email,
                    "phone": row["phone"] or phone,
                    "suburb": row["suburb"] or lead.get("suburb", ""),
                    "service": row["service"] or lead.get("service", ""),
                }
                score = max(int(row["score"]), int(lead.get("score", 0)))
                urgency = "urgent" if "urgent" in (row["urgency"], lead.get("urgency")) else row["urgency"]
                status = row["status"] if row["status"] not in ("lost", "spam") else "new"
                c.execute(
                    """UPDATE leads SET name=?,email=?,phone=?,suburb=?,service=?,score=?,urgency=?,status=?,
                       next_action=?,updated_at=? WHERE id=?""",
                    (
                        merged["name"], merged["email"], merged["phone"], merged["suburb"], merged["service"],
                        score, urgency, status, lead.get("next_action", row["next_action"]), ts, lead_id,
                    ),
                )
            c.execute(
                """INSERT OR IGNORE INTO enquiries (lead_id,source,external_id,subject,body,thread_id,category,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    lead_id, enquiry.get("source", ""), enquiry.get("external_id", ""), enquiry.get("subject", ""),
                    enquiry.get("body", ""), enquiry.get("thread_id", ""), enquiry.get("category", ""), ts,
                ),
            )
        return lead_id, created

    def is_processed(self, source: str, external_id: str) -> bool:
        with self.tx() as c:
            row = c.execute(
                "SELECT 1 FROM processed WHERE source=? AND external_id=?", (source, external_id)
            ).fetchone()
        return row is not None

    def mark_processed(self, source: str, external_id: str, category: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO processed (source,external_id,category,created_at) VALUES (?,?,?,?)",
                (source, external_id, category, now_iso()),
            )

    def get_lead(self, lead_id: int) -> dict[str, Any] | None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return dict(row) if row else None

    def list_leads(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.tx() as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM leads WHERE status=? ORDER BY score DESC, id DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def lead_enquiries(self, lead_id: int) -> list[dict[str, Any]]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM enquiries WHERE lead_id=? ORDER BY id", (lead_id,)).fetchall()
        return [dict(r) for r in rows]

    def update_lead_status(self, lead_id: int, status: str) -> bool:
        if status not in LEAD_STATUSES:
            raise ValueError(f"invalid lead status {status!r}")
        with self.tx() as c:
            cur = c.execute("UPDATE leads SET status=?, updated_at=? WHERE id=?", (status, now_iso(), lead_id))
        return cur.rowcount == 1

    # ------------------------------------------------------------- approvals
    def create_approval(
        self, kind: str, title: str, payload: dict[str, Any], idempotency_key: str, lead_id: int | None = None
    ) -> tuple[int, bool]:
        """Create a pending approval. Returns (id, created). Duplicate keys are not re-created."""
        with self.tx() as c:
            row = c.execute("SELECT id FROM approvals WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                return int(row["id"]), False
            cur = c.execute(
                """INSERT INTO approvals (kind,title,payload,status,idempotency_key,lead_id,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (kind, title, json.dumps(payload), "pending", idempotency_key, lead_id, now_iso()),
            )
            return int(cur.lastrowid), True

    def get_approval(self, approval_id: int) -> dict[str, Any] | None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    def list_approvals(self, status: str | None = "pending", limit: int = 100) -> list[dict[str, Any]]:
        with self.tx() as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM approvals WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def has_pending_approval(self, lead_id: int) -> bool:
        with self.tx() as c:
            row = c.execute(
                "SELECT 1 FROM approvals WHERE lead_id=? AND status='pending' LIMIT 1", (lead_id,)
            ).fetchone()
        return row is not None

    def claim_approval(self, approval_id: int, new_status: str, decided_by: str) -> bool:
        """Atomically move a *pending* approval to approved/rejected. False if not pending."""
        if new_status not in ("approved", "rejected"):
            raise ValueError("new_status must be approved or rejected")
        with self.tx() as c:
            cur = c.execute(
                "UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'",
                (new_status, decided_by, now_iso(), approval_id),
            )
        return cur.rowcount == 1

    def finish_approval(self, approval_id: int, status: str, result: str) -> None:
        if status not in ("executed", "failed", "approved"):
            raise ValueError("status must be executed, failed or approved")
        with self.tx() as c:
            c.execute("UPDATE approvals SET status=?, result=? WHERE id=?", (status, result[:2000], approval_id))

    # --------------------------------------------------------------- actions
    def log_action(self, agent: str, action: str, status: str, detail: str = "", run_id: int | None = None) -> None:
        if status not in ACTION_STATUSES:
            raise ValueError(f"invalid action status {status!r}")
        with self.tx() as c:
            c.execute(
                "INSERT INTO actions (run_id,agent,action,status,detail,created_at) VALUES (?,?,?,?,?,?)",
                (run_id, agent, action, status, detail[:2000], now_iso()),
            )

    def recent_actions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- content
    def add_content(
        self, kind: str, title: str, body: str, dedupe_key: str, status: str = "draft", scheduled_for: str = ""
    ) -> tuple[int, bool]:
        with self.tx() as c:
            row = c.execute("SELECT id FROM content WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            if row:
                return int(row["id"]), False
            cur = c.execute(
                """INSERT INTO content (kind,title,body,status,scheduled_for,dedupe_key,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (kind, title, body, status, scheduled_for, dedupe_key, now_iso()),
            )
            return int(cur.lastrowid), True

    def content_exists(self, dedupe_key: str) -> bool:
        with self.tx() as c:
            return c.execute("SELECT 1 FROM content WHERE dedupe_key=?", (dedupe_key,)).fetchone() is not None

    def update_content(self, content_id: int, status: str, external_id: str = "") -> None:
        with self.tx() as c:
            c.execute("UPDATE content SET status=?, external_id=? WHERE id=?", (status, external_id, content_id))

    def list_content(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM content ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- prospects
    def add_prospect(self, p: dict[str, Any]) -> tuple[int, bool]:
        org = (p.get("organisation") or "").strip()
        if not org:
            raise ValueError("organisation is required")
        email = (p.get("email") or "").strip().lower()
        with self.tx() as c:
            row = c.execute(
                "SELECT id FROM prospects WHERE organisation=? AND email=?", (org, email)
            ).fetchone()
            if row:
                return int(row["id"]), False
            cur = c.execute(
                """INSERT INTO prospects (organisation,contact_name,email,category,consent_basis,source_note,
                   status,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (org, p.get("contact_name", ""), email, p.get("category", ""), p.get("consent_basis", ""),
                 p.get("source_note", ""), "new", now_iso()),
            )
            return int(cur.lastrowid), True

    def list_prospects(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.tx() as c:
            if status:
                rows = c.execute("SELECT * FROM prospects WHERE status=? ORDER BY id", (status,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM prospects ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def update_prospect_status(self, prospect_id: int, status: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE prospects SET status=? WHERE id=?", (status, prospect_id))

    def stale_contacted_leads(self, older_than_days: int) -> list[dict[str, Any]]:
        with self.tx() as c:
            rows = c.execute(
                "SELECT * FROM leads WHERE status IN ('contacted','quoted') AND updated_at < "
                "strftime('%Y-%m-%dT%H:%M:%S+00:00','now',?) ORDER BY score DESC",
                (f"-{int(older_than_days)} days",),
            ).fetchall()
        return [dict(r) for r in rows]

    def touch_lead(self, lead_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE leads SET updated_at=? WHERE id=?", (now_iso(), lead_id))

    # ------------------------------------------------------------------ runs
    def start_run(self, trigger: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO runs (trigger,status,started_at) VALUES (?,?,?)", (trigger, "running", now_iso())
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, report: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET status=?, finished_at=?, report=? WHERE id=?",
                (status, now_iso(), json.dumps(report, default=str), run_id),
            )

    def last_run(self) -> dict[str, Any] | None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        d = dict(row)
        d["report"] = json.loads(d["report"]) if d["report"] else {}
        return d

    def counts(self) -> dict[str, int]:
        with self.tx() as c:
            return {
                "leads_new": c.execute("SELECT COUNT(*) FROM leads WHERE status='new'").fetchone()[0],
                "leads_total": c.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
                "approvals_pending": c.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0],
                "quotes_pending": c.execute(
                    "SELECT COUNT(*) FROM approvals WHERE status='pending' AND kind='quote'"
                ).fetchone()[0],
                "emails_pending": c.execute(
                    "SELECT COUNT(*) FROM approvals WHERE status='pending' AND kind='email_reply'"
                ).fetchone()[0],
                "actions_failed_24h": c.execute(
                    "SELECT COUNT(*) FROM actions WHERE status='failed' "
                    "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S+00:00','now','-1 day')"
                ).fetchone()[0],
            }
