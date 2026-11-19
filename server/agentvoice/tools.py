"""Function tools the voice agent can call during a conversation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .compliance import primary_timezone, us_holidays, within_window
from .db import Database, to_iso, utcnow

log = logging.getLogger("agentvoice.tools")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
SLOT_MINUTES = 30

OUTCOMES = (
    "interested",
    "not_interested",
    "wrong_number",
    "gatekeeper",
    "callback_requested",
    "already_has_provider",
    "voicemail",
    "other",
)
OUTCOME_TO_STATUS = {
    "interested": "interested",
    "not_interested": "not_interested",
    "wrong_number": "wrong_number",
    "already_has_provider": "not_interested",
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "record_lead_info",
        "description": "Save facts learned about the prospect during the call. Call it whenever you learn something useful.",
        "parameters": {
            "type": "object",
            "properties": {
                "contact_name": {"type": "string", "description": "Full name of the person you're talking to"},
                "role": {"type": "string", "description": "e.g. owner, manager, office admin"},
                "email": {"type": "string"},
                "current_website": {"type": "string"},
                "how_they_get_customers": {"type": "string"},
                "pain_points": {"type": "string"},
                "goals": {"type": "string"},
                "interested_in": {"type": "string", "description": "website, redesign, online store, mobile app, SEO..."},
                "budget": {"type": "string"},
                "timeline": {"type": "string"},
                "decision_makers": {"type": "string"},
                "interest_level": {"type": "string", "enum": ["hot", "warm", "cold"]},
                "notes": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "check_availability",
        "description": "Get open slots for the strategy call. Times are returned in the prospect's time zone.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD in the prospect's time zone. Omit for the next few business days."},
            },
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "book_meeting",
        "description": "Book the strategy call after the prospect agreed to a specific slot and confirmed name and email.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD in the prospect's time zone"},
                "time": {"type": "string", "description": "HH:MM 24-hour, prospect's time zone"},
                "contact_name": {"type": "string"},
                "email": {"type": "string"},
                "notes": {"type": "string", "description": "What they want to discuss, key needs"},
            },
            "required": ["date", "time", "contact_name", "email"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "schedule_callback",
        "description": "Schedule a call back at a time the prospect asked for.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD, prospect's time zone"},
                "time": {"type": "string", "description": "HH:MM 24-hour, prospect's time zone"},
                "ask_for": {"type": "string", "description": "Who to ask for next time"},
                "reason": {"type": "string"},
            },
            "required": ["date", "time"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "set_outcome",
        "description": "Record the outcome of the call once it's clear.",
        "parameters": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": list(OUTCOMES)},
                "notes": {"type": "string"},
            },
            "required": ["outcome"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "add_to_do_not_call",
        "description": "Immediately add this number to the do-not-call list when the person asks not to be called.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "transfer_to_human",
        "description": "Transfer the call to a human team member. Only when the prospect explicitly asks for a person.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "end_call",
        "description": "Hang up. Say your goodbye first in the same turn; the line closes after your audio finishes.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
]


class CallHost(Protocol):
    def request_hangup(self, reason: str) -> None: ...

    async def request_transfer(self, number: str, reason: str) -> None: ...


_TZ_NAMES = {
    "EST": "Eastern", "EDT": "Eastern", "CST": "Central", "CDT": "Central",
    "MST": "Mountain", "MDT": "Mountain", "PST": "Pacific", "PDT": "Pacific",
    "AKST": "Alaska", "AKDT": "Alaska", "HST": "Hawaii", "AST": "Atlantic",
}


def spoken_time(dt: datetime) -> str:
    hour = dt.strftime("%I").lstrip("0")
    minute = "" if dt.minute == 0 else f":{dt.minute:02d}"
    zone = _TZ_NAMES.get(dt.tzname() or "", dt.tzname() or "")
    return f"{dt:%A}, {dt:%B} {dt.day} at {hour}{minute} {dt:%p} {zone} time"


def _parse_local(day: str, hhmm: str, tz: ZoneInfo) -> datetime:
    d = date.fromisoformat(day.strip())
    t = time.fromisoformat(hhmm.strip()[:5])
    return datetime.combine(d, t, tzinfo=tz)


def open_slots(
    db: Database,
    agent_settings: dict[str, Any],
    prospect_tz: str,
    day: date | None = None,
    now_utc: datetime | None = None,
    meeting_minutes: int = 20,
    limit: int = 6,
) -> list[datetime]:
    """Open meeting slots (as aware datetimes in the prospect's zone)."""
    now_utc = now_utc or utcnow()
    seller_tz = ZoneInfo(agent_settings["meeting_timezone"])
    p_tz = ZoneInfo(prospect_tz)
    start_t = time.fromisoformat(agent_settings["meeting_hours_start"])
    end_t = time.fromisoformat(agent_settings["meeting_hours_end"])
    earliest = now_utc + timedelta(hours=int(agent_settings["meeting_min_notice_hours"]))

    if day is not None:
        seller_days = [day - timedelta(days=1), day, day + timedelta(days=1)]
    else:
        first = now_utc.astimezone(seller_tz).date()
        seller_days = [first + timedelta(days=i) for i in range(0, 14)]

    window_start = datetime.combine(seller_days[0], time(0), tzinfo=seller_tz)
    window_end = datetime.combine(seller_days[-1] + timedelta(days=1), time(0), tzinfo=seller_tz)
    booked = db.meetings_between(window_start, window_end)
    busy = []
    for m in booked:
        s = datetime.fromisoformat(m["starts_at"])
        busy.append((s, s + timedelta(minutes=int(m["duration_minutes"]))))

    slots: list[datetime] = []
    per_day: dict[date, int] = {}
    for sd in seller_days:
        if sd.weekday() not in agent_settings["meeting_days"] or sd in us_holidays(sd.year):
            continue
        cursor = datetime.combine(sd, start_t, tzinfo=seller_tz)
        day_end = datetime.combine(sd, end_t, tzinfo=seller_tz)
        while cursor + timedelta(minutes=meeting_minutes) <= day_end:
            slot_end = cursor + timedelta(minutes=meeting_minutes)
            local = cursor.astimezone(p_tz)
            free = cursor >= earliest and not any(s < slot_end and cursor < e for s, e in busy)
            reasonable_local = time(8) <= local.time() <= time(18, 30)
            if free and reasonable_local and (day is None or local.date() == day):
                if day is None:
                    # Spread suggestions: at most 2 per day when no date was requested.
                    if per_day.get(local.date(), 0) < 2:
                        slots.append(local)
                        per_day[local.date()] = per_day.get(local.date(), 0) + 1
                else:
                    slots.append(local)
            cursor += timedelta(minutes=SLOT_MINUTES)
        if len(slots) >= limit and day is None:
            break
    if day is None:
        return slots[:limit]
    # For a specific day, offer a spread: morning, midday, afternoon.
    if len(slots) > limit:
        step = len(slots) / limit
        slots = [slots[int(i * step)] for i in range(limit)]
    return slots


class ToolRunner:
    def __init__(
        self,
        *,
        db: Database,
        host: CallHost,
        call_id: int,
        lead: dict[str, Any] | None,
        agent_settings: dict[str, Any],
        playbook: dict[str, Any],
        dry_run: bool = False,
    ):
        self.db = db
        self.host = host
        self.call_id = call_id
        self.lead = lead
        self.settings = agent_settings
        self.playbook = playbook
        self.dry_run = dry_run or lead is None
        self.prospect_tz = primary_timezone(lead) if lead and lead.get("phone") else "America/New_York"
        self.outcome_set = False
        self._background: set[asyncio.Task[Any]] = set()

    async def run(self, name: str, raw_args: str) -> dict[str, Any]:
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "error": "arguments were not valid JSON"}
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"ok": False, "error": f"unknown tool {name}"}
        self.db.add_transcript(self.call_id, "tool", f"{name}({json.dumps(args, ensure_ascii=False)})")
        try:
            result = await handler(**args)
        except TypeError as exc:
            result = {"ok": False, "error": f"bad arguments: {exc}"}
        except Exception as exc:  # noqa: BLE001 - the model must always get an answer
            log.exception("tool %s failed", name)
            result = {"ok": False, "error": str(exc)}
        return result

    # -------------------------------------------------------------- handlers
    async def _tool_record_lead_info(self, **info: Any) -> dict[str, Any]:
        info = {k: str(v).strip() for k, v in info.items() if v not in (None, "")}
        if self.dry_run:
            return {"ok": True, "saved": list(info)}
        assert self.lead is not None
        updates: dict[str, Any] = {}
        email = info.get("email", "")
        if email:
            if not EMAIL_RE.match(email):
                return {"ok": False, "error": "that email doesn't look valid; confirm it with the prospect"}
            updates["email"] = email
        if info.get("contact_name"):
            updates["contact_name"] = info["contact_name"]
        if info.get("interest_level"):
            updates["score"] = info["interest_level"]
        if updates:
            self.db.update_lead(self.lead["id"], **updates)
        self.db.merge_lead_json(self.lead["id"], "qualification", info)
        return {"ok": True, "saved": list(info)}

    async def _tool_check_availability(self, date: str | None = None) -> dict[str, Any]:
        day = None
        if date:
            try:
                day = datetime.fromisoformat(date.strip()[:10]).date()
            except ValueError:
                return {"ok": False, "error": "date must be YYYY-MM-DD"}
        slots = open_slots(
            self.db,
            self.settings,
            self.prospect_tz,
            day,
            meeting_minutes=int(self.playbook.get("meeting_length_minutes", 20)),
        )
        if not slots:
            return {"ok": True, "slots": [], "message": "No open slots that day. Try another date."}
        return {
            "ok": True,
            "timezone": self.prospect_tz,
            "slots": [{"date": s.strftime("%Y-%m-%d"), "time": s.strftime("%H:%M"), "say": spoken_time(s)} for s in slots],
            "hint": "Offer two of these, not the whole list.",
        }

    async def _tool_book_meeting(
        self, date: str, time: str, contact_name: str, email: str, notes: str = ""
    ) -> dict[str, Any]:
        email = email.strip().replace(" ", "")
        if not EMAIL_RE.match(email):
            return {"ok": False, "error": "invalid email; ask them to spell it again"}
        tz = ZoneInfo(self.prospect_tz)
        try:
            start = _parse_local(date, time, tz)
        except ValueError:
            return {"ok": False, "error": "date must be YYYY-MM-DD and time HH:MM"}
        minutes = int(self.playbook.get("meeting_length_minutes", 20))
        available = open_slots(self.db, self.settings, self.prospect_tz, start.date(), meeting_minutes=minutes, limit=100)
        if not any(s == start for s in available):
            alternatives = [spoken_time(s) for s in available[:: max(1, len(available) // 3)][:3]]
            return {"ok": False, "error": "that time isn't available", "alternatives": alternatives}

        confirmation = spoken_time(start)
        if self.dry_run:
            return {"ok": True, "confirmed": confirmation, "note": "practice mode, nothing was saved"}
        assert self.lead is not None
        meeting = self.db.create_meeting(
            lead_id=self.lead["id"],
            call_id=self.call_id,
            starts_at=to_iso(start),
            duration_minutes=minutes,
            timezone=self.prospect_tz,
            contact_name=contact_name.strip(),
            email=email,
            phone=self.lead["phone"],
            notes=notes.strip(),
        )
        self.db.update_lead(self.lead["id"], status="meeting_booked", email=email, contact_name=contact_name.strip(), score="hot")
        self.db.update_call(self.call_id, outcome="meeting_booked")
        self.outcome_set = True
        self._fire_webhook(meeting)
        return {"ok": True, "confirmed": confirmation, "email": email}

    async def _tool_schedule_callback(
        self, date: str, time: str, ask_for: str = "", reason: str = ""
    ) -> dict[str, Any]:
        tz = ZoneInfo(self.prospect_tz)
        try:
            when = _parse_local(date, time, tz)
        except ValueError:
            return {"ok": False, "error": "date must be YYYY-MM-DD and time HH:MM"}
        if when <= utcnow():
            return {"ok": False, "error": "that time is in the past"}
        ok, why = within_window(when.astimezone(ZoneInfo("UTC")), [self.prospect_tz], self.settings)
        if not ok:
            return {
                "ok": False,
                "error": f"we can only call between {self.settings['call_window_start']} and "
                f"{self.settings['call_window_end']} on business days ({why}); suggest another time",
            }
        if self.dry_run:
            return {"ok": True, "scheduled": spoken_time(when)}
        assert self.lead is not None
        note = "; ".join(x for x in (f"ask for {ask_for}" if ask_for else "", reason) if x)
        self.db.update_lead(self.lead["id"], status="callback", next_attempt_at=to_iso(when), status_reason=note)
        if ask_for:
            self.db.merge_lead_json(self.lead["id"], "qualification", {"ask_for": ask_for})
        self.db.update_call(self.call_id, outcome="callback_scheduled", next_step=f"Call back {spoken_time(when)}")
        self.outcome_set = True
        return {"ok": True, "scheduled": spoken_time(when)}

    async def _tool_set_outcome(self, outcome: str, notes: str = "") -> dict[str, Any]:
        if outcome not in OUTCOMES:
            return {"ok": False, "error": f"outcome must be one of {', '.join(OUTCOMES)}"}
        if self.dry_run:
            return {"ok": True}
        assert self.lead is not None
        call = self.db.get_call(self.call_id) or {}
        if call.get("outcome") in ("meeting_booked", "do_not_call", "callback_scheduled"):
            return {"ok": True, "note": f"keeping existing outcome {call['outcome']}"}
        self.db.update_call(self.call_id, outcome=outcome)
        status = OUTCOME_TO_STATUS.get(outcome)
        if status:
            self.db.update_lead(self.lead["id"], status=status, status_reason=notes[:300])
        if notes:
            self.db.merge_lead_json(self.lead["id"], "qualification", {"call_notes": notes})
        self.outcome_set = True
        return {"ok": True}

    async def _tool_add_to_do_not_call(self, reason: str = "") -> dict[str, Any]:
        if self.dry_run:
            return {"ok": True, "message": "Number removed (practice). Say goodbye and end the call."}
        assert self.lead is not None
        self.db.add_dnc(self.lead["phone"], reason or "requested on call", source="call")
        self.db.update_call(self.call_id, outcome="do_not_call")
        self.outcome_set = True
        return {"ok": True, "message": "Number removed. Confirm briefly, say goodbye, then end_call."}

    async def _tool_transfer_to_human(self, reason: str = "") -> dict[str, Any]:
        number = self.settings.get("transfer_number")
        if not number:
            return {"ok": False, "error": "no human is available; offer a meeting or a callback instead"}
        if self.dry_run:
            return {"ok": True, "message": "In a real call the line would transfer now."}
        await self.host.request_transfer(number, reason)
        self.db.update_call(self.call_id, outcome="transferred")
        self.outcome_set = True
        return {"ok": True, "message": "Transferring now. Say one short sentence like 'connecting you now'."}

    async def _tool_end_call(self, reason: str = "") -> dict[str, Any]:
        self.host.request_hangup(reason)
        return {"ok": True}

    # --------------------------------------------------------------- helpers
    def _fire_webhook(self, meeting: dict[str, Any]) -> None:
        url = self.settings.get("booking_webhook_url")
        if not url:
            return
        payload = {"event": "meeting.booked", "meeting": meeting, "lead": self.db.get_lead(self.lead["id"]) if self.lead else None}

        async def send() -> None:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                log.warning("booking webhook failed: %s", exc)

        task = asyncio.create_task(send())
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def validate_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False
