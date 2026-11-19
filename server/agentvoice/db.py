from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'paused',
    extra_instructions TEXT NOT NULL DEFAULT '',
    max_concurrent INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    retry_minutes INTEGER NOT NULL DEFAULT 1440,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    phone TEXT NOT NULL,
    business_name TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT '',
    consent INTEGER NOT NULL DEFAULT 0,
    consent_source TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    status_reason TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    qualification TEXT NOT NULL DEFAULT '{}',
    research TEXT NOT NULL DEFAULT '{}',
    score TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (campaign_id, phone)
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(campaign_id, status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    direction TEXT NOT NULL DEFAULT 'outbound',
    kind TEXT NOT NULL DEFAULT 'campaign',
    phone TEXT NOT NULL DEFAULT '',
    twilio_sid TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    answered_by TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    lead_score TEXT NOT NULL DEFAULT '',
    next_step TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    stream_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    answered_at TEXT,
    ended_at TEXT,
    duration_s INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calls_lead ON calls(lead_id);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
CREATE INDEX IF NOT EXISTS idx_calls_sid ON calls(twilio_sid);

CREATE TABLE IF NOT EXISTS transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcript_call ON transcript(call_id);

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    call_id INTEGER REFERENCES calls(id) ON DELETE SET NULL,
    starts_at TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT 20,
    timezone TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'booked',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meetings_start ON meetings(starts_at);

CREATE TABLE IF NOT EXISTS dnc (
    phone TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

ACTIVE_CALL_STATUSES = ("queued", "initiated", "ringing", "in-progress")
DIALABLE_LEAD_STATUSES = ("new", "retry", "callback")
LEAD_STATUSES = (
    "new",
    "calling",
    "retry",
    "callback",
    "contacted",
    "interested",
    "meeting_booked",
    "not_interested",
    "do_not_call",
    "wrong_number",
    "invalid",
    "exhausted",
    "blocked",
)

LEAD_FIELDS = (
    "campaign_id",
    "phone",
    "business_name",
    "contact_name",
    "email",
    "website",
    "industry",
    "city",
    "state",
    "timezone",
    "consent",
    "consent_source",
    "notes",
    "status",
    "status_reason",
    "next_attempt_at",
    "score",
)
CAMPAIGN_FIELDS = ("name", "status", "extra_instructions", "max_concurrent", "max_attempts", "retry_minutes")
CALL_FIELDS = (
    "twilio_sid",
    "status",
    "answered_by",
    "outcome",
    "summary",
    "lead_score",
    "next_step",
    "error",
    "answered_at",
    "ended_at",
    "duration_s",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key in ("qualification", "research"):
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key] or "{}")
            except json.JSONDecodeError:
                data[key] = {}
    if "consent" in data:
        data["consent"] = bool(data["consent"])
    return data


class Database:
    """Thread-safe SQLite wrapper. Every operation is short, so a single lock is enough."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            return _row(self._conn.execute(sql, tuple(params)).fetchone())

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [_row(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]  # type: ignore[misc]

    def _update(self, table: str, row_id: int, allowed: Iterable[str], fields: dict[str, Any], touch: bool) -> None:
        allowed = set(allowed)
        data = {k: v for k, v in fields.items() if k in allowed}
        if touch:
            data["updated_at"] = now_iso()
        if not data:
            return
        if "consent" in data:
            data["consent"] = 1 if data["consent"] else 0
        cols = ", ".join(f"{k} = ?" for k in data)
        self._exec(f"UPDATE {table} SET {cols} WHERE id = ?", [*data.values(), row_id])

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM settings WHERE key = ?", [key])
        if row is None:
            return default
        return json.loads(row["value"])

    def set_setting(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [key, json.dumps(value)],
        )

    # --------------------------------------------------------------- campaigns
    def create_campaign(self, name: str, **fields: Any) -> dict[str, Any]:
        ts = now_iso()
        cur = self._exec(
            "INSERT INTO campaigns(name, created_at, updated_at) VALUES(?, ?, ?)", [name, ts, ts]
        )
        cid = int(cur.lastrowid)
        if fields:
            self._update("campaigns", cid, CAMPAIGN_FIELDS, fields, touch=True)
        return self.get_campaign(cid)  # type: ignore[return-value]

    def get_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM campaigns WHERE id = ?", [campaign_id])

    def list_campaigns(self) -> list[dict[str, Any]]:
        return self._all(
            """
            SELECT c.*,
                (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id) AS lead_count,
                (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id
                    AND l.status IN ('new','retry','callback')) AS remaining,
                (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id
                    AND l.status = 'meeting_booked') AS meetings
            FROM campaigns c ORDER BY c.id DESC
            """
        )

    def running_campaigns(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM campaigns WHERE status = 'running' ORDER BY id")

    def update_campaign(self, campaign_id: int, **fields: Any) -> dict[str, Any] | None:
        self._update("campaigns", campaign_id, CAMPAIGN_FIELDS, fields, touch=True)
        return self.get_campaign(campaign_id)

    def delete_campaign(self, campaign_id: int) -> None:
        self._exec("DELETE FROM leads WHERE campaign_id = ? AND status = 'new' AND attempts = 0", [campaign_id])
        self._exec("DELETE FROM campaigns WHERE id = ?", [campaign_id])

    # ------------------------------------------------------------------- leads
    def create_lead(self, phone: str, **fields: Any) -> dict[str, Any]:
        ts = now_iso()
        cur = self._exec(
            "INSERT INTO leads(phone, campaign_id, created_at, updated_at) VALUES(?, ?, ?, ?)",
            [phone, fields.get("campaign_id"), ts, ts],
        )
        lid = int(cur.lastrowid)
        rest = {k: v for k, v in fields.items() if k != "campaign_id"}
        if rest:
            self._update("leads", lid, LEAD_FIELDS, rest, touch=False)
        return self.get_lead(lid)  # type: ignore[return-value]

    def find_lead(self, campaign_id: int | None, phone: str) -> dict[str, Any] | None:
        if campaign_id is None:
            return self._one("SELECT * FROM leads WHERE campaign_id IS NULL AND phone = ?", [phone])
        return self._one("SELECT * FROM leads WHERE campaign_id = ? AND phone = ?", [campaign_id, phone])

    def find_lead_by_phone(self, phone: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM leads WHERE phone = ? ORDER BY updated_at DESC LIMIT 1", [phone])

    def get_lead(self, lead_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM leads WHERE id = ?", [lead_id])

    def update_lead(self, lead_id: int, **fields: Any) -> dict[str, Any] | None:
        self._update("leads", lead_id, LEAD_FIELDS, fields, touch=True)
        return self.get_lead(lead_id)

    def merge_lead_json(self, lead_id: int, column: str, data: dict[str, Any]) -> None:
        if column not in ("qualification", "research"):
            raise ValueError(column)
        with self._lock:
            lead = self.get_lead(lead_id)
            if lead is None:
                return
            merged = dict(lead[column]) if column == "qualification" else {}
            merged.update({k: v for k, v in data.items() if v not in (None, "")})
            self._conn.execute(
                f"UPDATE leads SET {column} = ?, updated_at = ? WHERE id = ?",
                [json.dumps(merged), now_iso(), lead_id],
            )

    def delete_lead(self, lead_id: int) -> None:
        self._exec("DELETE FROM leads WHERE id = ?", [lead_id])

    def list_leads(
        self,
        campaign_id: int | None = None,
        status: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where, params = [], []
        if campaign_id is not None:
            where.append("campaign_id = ?")
            params.append(campaign_id)
        if status:
            where.append("status = ?")
            params.append(status)
        if query:
            like = f"%{query}%"
            where.append(
                "(phone LIKE ? OR business_name LIKE ? OR contact_name LIKE ? OR city LIKE ? OR industry LIKE ?)"
            )
            params.extend([like] * 5)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self._one(f"SELECT COUNT(*) AS n FROM leads {clause}", params)["n"]  # type: ignore[index]
        rows = self._all(
            f"SELECT * FROM leads {clause} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return rows, total

    def dialable_leads(self, campaign: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        return self._all(
            f"""
            SELECT * FROM leads
            WHERE campaign_id = ?
              AND status IN ({",".join("?" * len(DIALABLE_LEAD_STATUSES))})
              AND attempts < ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY CASE status WHEN 'callback' THEN 0 WHEN 'retry' THEN 1 ELSE 2 END,
                     next_attempt_at, id
            LIMIT ?
            """,
            [campaign["id"], *DIALABLE_LEAD_STATUSES, campaign["max_attempts"], now_iso(), limit],
        )

    def calls_to_phone_since(self, phone: str, since: datetime) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM calls WHERE phone = ? AND direction = 'outbound' "
            "AND kind != 'test' AND created_at >= ?",
            [phone, to_iso(since)],
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------- calls
    def create_call(
        self,
        *,
        phone: str,
        lead_id: int | None,
        campaign_id: int | None,
        direction: str = "outbound",
        kind: str = "campaign",
    ) -> dict[str, Any]:
        cur = self._exec(
            "INSERT INTO calls(lead_id, campaign_id, direction, kind, phone, stream_token, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            [lead_id, campaign_id, direction, kind, phone, secrets.token_urlsafe(24), now_iso()],
        )
        return self.get_call(int(cur.lastrowid))  # type: ignore[return-value]

    def get_call(self, call_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM calls WHERE id = ?", [call_id])

    def get_call_by_sid(self, sid: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM calls WHERE twilio_sid = ?", [sid])

    def update_call(self, call_id: int, **fields: Any) -> dict[str, Any] | None:
        self._update("calls", call_id, CALL_FIELDS, fields, touch=False)
        return self.get_call(call_id)

    def list_calls(
        self, lead_id: int | None = None, limit: int = 100, offset: int = 0, active_only: bool = False
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if lead_id is not None:
            where.append("c.lead_id = ?")
            params.append(lead_id)
        if active_only:
            where.append(f"c.status IN ({','.join('?' * len(ACTIVE_CALL_STATUSES))})")
            params.extend(ACTIVE_CALL_STATUSES)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        return self._all(
            f"""
            SELECT c.id, c.lead_id, c.campaign_id, c.direction, c.kind, c.phone, c.twilio_sid,
                   c.status, c.answered_by, c.outcome, c.summary, c.lead_score, c.next_step,
                   c.error, c.created_at, c.answered_at, c.ended_at, c.duration_s,
                   l.business_name, l.contact_name
            FROM calls c LEFT JOIN leads l ON l.id = c.lead_id
            {clause} ORDER BY c.id DESC LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )

    def count_active_calls(self, campaign_id: int | None = None) -> int:
        params: list[Any] = list(ACTIVE_CALL_STATUSES)
        sql = f"SELECT COUNT(*) AS n FROM calls WHERE status IN ({','.join('?' * len(ACTIVE_CALL_STATUSES))})"
        if campaign_id is not None:
            sql += " AND campaign_id = ?"
            params.append(campaign_id)
        row = self._one(sql, params)
        return int(row["n"]) if row else 0

    def expire_stale_calls(self, older_than: timedelta) -> int:
        cutoff = to_iso(utcnow() - older_than)
        cur = self._exec(
            f"UPDATE calls SET status = 'failed', error = 'stale: no final status received', ended_at = ? "
            f"WHERE status IN ({','.join('?' * len(ACTIVE_CALL_STATUSES))}) AND created_at < ?",
            [now_iso(), *ACTIVE_CALL_STATUSES, cutoff],
        )
        return cur.rowcount

    # -------------------------------------------------------------- transcript
    def add_transcript(self, call_id: int, role: str, text: str) -> None:
        text = text.strip()
        if text:
            self._exec(
                "INSERT INTO transcript(call_id, role, text, at) VALUES(?, ?, ?, ?)",
                [call_id, role, text, now_iso()],
            )

    def get_transcript(self, call_id: int) -> list[dict[str, Any]]:
        return self._all("SELECT role, text, at FROM transcript WHERE call_id = ? ORDER BY id", [call_id])

    # ---------------------------------------------------------------- meetings
    def create_meeting(self, **fields: Any) -> dict[str, Any]:
        cols = (
            "lead_id",
            "call_id",
            "starts_at",
            "duration_minutes",
            "timezone",
            "contact_name",
            "email",
            "phone",
            "notes",
        )
        data = {k: fields.get(k) for k in cols if fields.get(k) is not None}
        data["created_at"] = now_iso()
        cur = self._exec(
            f"INSERT INTO meetings({', '.join(data)}) VALUES({', '.join('?' * len(data))})",
            list(data.values()),
        )
        return self._one("SELECT * FROM meetings WHERE id = ?", [cur.lastrowid])  # type: ignore[return-value]

    def list_meetings(self, upcoming_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        clause = "WHERE m.starts_at >= ? AND m.status = 'booked'" if upcoming_only else ""
        params: list[Any] = [now_iso()] if upcoming_only else []
        return self._all(
            f"""
            SELECT m.*, l.business_name, l.website, l.city, l.state
            FROM meetings m LEFT JOIN leads l ON l.id = m.lead_id
            {clause} ORDER BY m.starts_at {'ASC' if upcoming_only else 'DESC'} LIMIT ?
            """,
            [*params, limit],
        )

    def meetings_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM meetings WHERE status = 'booked' AND starts_at >= ? AND starts_at < ?",
            [to_iso(start - timedelta(hours=2)), to_iso(end)],
        )

    def update_meeting(self, meeting_id: int, status: str) -> dict[str, Any] | None:
        self._exec("UPDATE meetings SET status = ? WHERE id = ?", [status, meeting_id])
        return self._one("SELECT * FROM meetings WHERE id = ?", [meeting_id])

    # --------------------------------------------------------------------- dnc
    def is_dnc(self, phone: str) -> bool:
        return self._one("SELECT 1 AS x FROM dnc WHERE phone = ?", [phone]) is not None

    def add_dnc(self, phone: str, reason: str = "", source: str = "manual") -> None:
        self._exec(
            "INSERT INTO dnc(phone, reason, source, added_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(phone) DO NOTHING",
            [phone, reason, source, now_iso()],
        )
        self._exec(
            "UPDATE leads SET status = 'do_not_call', status_reason = ?, updated_at = ? "
            "WHERE phone = ? AND status NOT IN ('meeting_booked')",
            [reason or "on do-not-call list", now_iso(), phone],
        )

    def add_dnc_many(self, phones: Iterable[str], source: str) -> int:
        ts = now_iso()
        rows = [(p, "", source, ts) for p in phones]
        with self._lock:
            before = self._conn.total_changes
            self._conn.execute("BEGIN")
            self._conn.executemany(
                "INSERT INTO dnc(phone, reason, source, added_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(phone) DO NOTHING",
                rows,
            )
            self._conn.execute(
                "UPDATE leads SET status = 'do_not_call', status_reason = 'on do-not-call list', updated_at = ? "
                "WHERE status IN ('new','retry','callback') AND phone IN (SELECT phone FROM dnc)",
                [ts],
            )
            self._conn.execute("COMMIT")
            return self._conn.total_changes - before

    def remove_dnc(self, phone: str) -> None:
        self._exec("DELETE FROM dnc WHERE phone = ?", [phone])

    def list_dnc(self, limit: int = 500, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        total = self._one("SELECT COUNT(*) AS n FROM dnc")["n"]  # type: ignore[index]
        rows = self._all("SELECT * FROM dnc ORDER BY added_at DESC LIMIT ? OFFSET ?", [limit, offset])
        return rows, total

    # ------------------------------------------------------------------- stats
    def stats(self, since: datetime) -> dict[str, Any]:
        s = to_iso(since)
        calls = self._one(
            """
            SELECT COUNT(*) AS dials,
                   SUM(CASE WHEN answered_by = 'human' OR (answered_at IS NOT NULL AND answered_by = '')
                       THEN 1 ELSE 0 END) AS conversations,
                   SUM(CASE WHEN answered_by LIKE 'machine%' THEN 1 ELSE 0 END) AS voicemails,
                   COALESCE(SUM(duration_s), 0) AS talk_seconds
            FROM calls WHERE created_at >= ? AND kind != 'practice'
            """,
            [s],
        ) or {}
        meetings = self._one("SELECT COUNT(*) AS n FROM meetings WHERE created_at >= ?", [s]) or {"n": 0}
        lead_status = self._all("SELECT status, COUNT(*) AS n FROM leads GROUP BY status")
        return {
            "since": s,
            "dials": int(calls.get("dials") or 0),
            "conversations": int(calls.get("conversations") or 0),
            "voicemails": int(calls.get("voicemails") or 0),
            "talk_minutes": round(int(calls.get("talk_seconds") or 0) / 60, 1),
            "meetings_booked": int(meetings["n"]),
            "active_calls": self.count_active_calls(),
            "leads_by_status": {r["status"]: r["n"] for r in lead_status},
        }
