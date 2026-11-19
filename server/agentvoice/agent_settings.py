from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database

VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")

DEFAULT_AGENT_SETTINGS: dict[str, Any] = {
    # Voice
    "voice": "marin",
    "speech_speed": 1.0,
    "turn_eagerness": "auto",
    # Calling rules (local time of the prospect)
    "call_window_start": "09:00",
    "call_window_end": "18:00",
    "call_days": [0, 1, 2, 3, 4],
    "skip_us_holidays": True,
    "require_consent": True,
    "max_calls_per_phone_per_day": 1,
    "dial_interval_seconds": 15,
    # Call behaviour
    "voicemail_action": "hangup",
    "max_call_minutes": 12,
    "silence_kickoff_seconds": 3.0,
    "transfer_number": "",
    "test_phone_number": "",
    # Meetings (in the seller's time zone)
    "meeting_timezone": "America/New_York",
    "meeting_hours_start": "10:00",
    "meeting_hours_end": "17:00",
    "meeting_days": [0, 1, 2, 3, 4],
    "meeting_min_notice_hours": 3,
    "booking_webhook_url": "",
}

# Hard outer bounds for the calling window. Federal TCPA allows 8am-9pm local, but several
# states (e.g. FL, OK, MD, WA) cap at 8pm, so we never allow a window outside 08:00-20:00.
HARD_WINDOW_START = "08:00"
HARD_WINDOW_END = "20:00"

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingsError(ValueError):
    pass


def _check_hhmm(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _HHMM.match(value):
        raise SettingsError(f"{name} must be HH:MM (24h)")
    return value


def _check_days(name: str, value: Any) -> list[int]:
    if not isinstance(value, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in value):
        raise SettingsError(f"{name} must be a list of weekday numbers 0 (Mon) .. 6 (Sun)")
    return sorted(set(value))


def validate_agent_settings(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULT_AGENT_SETTINGS)
    unknown = set(data) - set(DEFAULT_AGENT_SETTINGS)
    if unknown:
        raise SettingsError(f"unknown settings: {', '.join(sorted(unknown))}")
    out.update(data)

    if out["voice"] not in VOICES:
        raise SettingsError(f"voice must be one of {', '.join(VOICES)}")
    out["speech_speed"] = float(out["speech_speed"])
    if not 0.8 <= out["speech_speed"] <= 1.2:
        raise SettingsError("speech_speed must be between 0.8 and 1.2")
    if out["turn_eagerness"] not in ("auto", "low", "medium", "high"):
        raise SettingsError("turn_eagerness must be auto, low, medium or high")

    for key in ("call_window_start", "call_window_end", "meeting_hours_start", "meeting_hours_end"):
        _check_hhmm(key, out[key])
    if out["call_window_start"] < HARD_WINDOW_START or out["call_window_end"] > HARD_WINDOW_END:
        raise SettingsError(f"calling window must stay within {HARD_WINDOW_START}-{HARD_WINDOW_END} local time")
    if out["call_window_start"] >= out["call_window_end"]:
        raise SettingsError("call_window_start must be before call_window_end")
    if out["meeting_hours_start"] >= out["meeting_hours_end"]:
        raise SettingsError("meeting_hours_start must be before meeting_hours_end")
    out["call_days"] = _check_days("call_days", out["call_days"])
    out["meeting_days"] = _check_days("meeting_days", out["meeting_days"])

    for key in ("skip_us_holidays", "require_consent"):
        out[key] = bool(out[key])
    for key, lo, hi in (
        ("max_calls_per_phone_per_day", 1, 3),
        ("dial_interval_seconds", 3, 3600),
        ("max_call_minutes", 2, 30),
        ("meeting_min_notice_hours", 0, 72),
    ):
        out[key] = int(out[key])
        if not lo <= out[key] <= hi:
            raise SettingsError(f"{key} must be between {lo} and {hi}")
    out["silence_kickoff_seconds"] = float(out["silence_kickoff_seconds"])
    if not 1.0 <= out["silence_kickoff_seconds"] <= 10.0:
        raise SettingsError("silence_kickoff_seconds must be between 1 and 10")
    if out["voicemail_action"] not in ("hangup", "leave_message"):
        raise SettingsError("voicemail_action must be hangup or leave_message")
    try:
        ZoneInfo(out["meeting_timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SettingsError("meeting_timezone must be an IANA zone like America/New_York") from exc

    from .phones import normalize_any_phone

    for key in ("transfer_number", "test_phone_number"):
        if out[key]:
            normalized = normalize_any_phone(out[key])
            if normalized is None:
                raise SettingsError(f"{key} is not a valid phone number (use E.164 like +491701234567)")
            out[key] = normalized
    url = out["booking_webhook_url"]
    if url and not url.startswith(("https://", "http://")):
        raise SettingsError("booking_webhook_url must be an http(s) URL")
    return out


def get_agent_settings(db: Database) -> dict[str, Any]:
    stored = db.get_setting("agent_settings", {}) or {}
    merged = dict(DEFAULT_AGENT_SETTINGS)
    merged.update({k: v for k, v in stored.items() if k in DEFAULT_AGENT_SETTINGS})
    return merged


def save_agent_settings(db: Database, data: dict[str, Any]) -> dict[str, Any]:
    current = get_agent_settings(db)
    current.update(data)
    validated = validate_agent_settings(current)
    db.set_setting("agent_settings", validated)
    return validated
