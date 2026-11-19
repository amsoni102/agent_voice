"""Pre-dial checks for US outbound calling.

These checks are conservative engineering guard-rails, not legal advice. AI-generated voices
are "artificial voice" under the TCPA (FCC declaratory ruling, Feb 2024): calls to cell phones and
residential lines need prior express (written, for telemarketing) consent. Have counsel review
your lead sources, consent records and scripts before running campaigns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database, utcnow
from .phones import phone_timezones

STATE_TIMEZONES: dict[str, list[str]] = {
    "AL": ["America/Chicago"],
    "AK": ["America/Anchorage", "America/Adak"],
    "AZ": ["America/Phoenix"],
    "AR": ["America/Chicago"],
    "CA": ["America/Los_Angeles"],
    "CO": ["America/Denver"],
    "CT": ["America/New_York"],
    "DE": ["America/New_York"],
    "DC": ["America/New_York"],
    "FL": ["America/New_York", "America/Chicago"],
    "GA": ["America/New_York"],
    "HI": ["Pacific/Honolulu"],
    "ID": ["America/Boise", "America/Los_Angeles"],
    "IL": ["America/Chicago"],
    "IN": ["America/Indiana/Indianapolis", "America/Chicago"],
    "IA": ["America/Chicago"],
    "KS": ["America/Chicago", "America/Denver"],
    "KY": ["America/New_York", "America/Chicago"],
    "LA": ["America/Chicago"],
    "ME": ["America/New_York"],
    "MD": ["America/New_York"],
    "MA": ["America/New_York"],
    "MI": ["America/Detroit", "America/Menominee"],
    "MN": ["America/Chicago"],
    "MS": ["America/Chicago"],
    "MO": ["America/Chicago"],
    "MT": ["America/Denver"],
    "NE": ["America/Chicago", "America/Denver"],
    "NV": ["America/Los_Angeles"],
    "NH": ["America/New_York"],
    "NJ": ["America/New_York"],
    "NM": ["America/Denver"],
    "NY": ["America/New_York"],
    "NC": ["America/New_York"],
    "ND": ["America/Chicago", "America/Denver"],
    "OH": ["America/New_York"],
    "OK": ["America/Chicago"],
    "OR": ["America/Los_Angeles", "America/Boise"],
    "PA": ["America/New_York"],
    "RI": ["America/New_York"],
    "SC": ["America/New_York"],
    "SD": ["America/Chicago", "America/Denver"],
    "TN": ["America/Chicago", "America/New_York"],
    "TX": ["America/Chicago", "America/Denver"],
    "UT": ["America/Denver"],
    "VT": ["America/New_York"],
    "VA": ["America/New_York"],
    "WA": ["America/Los_Angeles"],
    "WV": ["America/New_York"],
    "WI": ["America/Chicago"],
    "WY": ["America/Denver"],
    "PR": ["America/Puerto_Rico"],
}

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR",
}


def normalize_state(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.upper() in STATE_TIMEZONES:
        return value.upper()
    return STATE_NAMES.get(value.lower(), "")


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reason: str = ""
    # Permanent failures mark the lead blocked; temporary ones (outside hours) just skip it for now.
    permanent: bool = False


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def us_holidays(year: int) -> set[date]:
    """Major US holidays when business owners are unlikely to welcome a sales call."""
    return {
        _observed(date(year, 1, 1)),
        _last_weekday(year, 5, 0),           # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),         # Labor Day
        _nth_weekday(year, 11, 3, 4),        # Thanksgiving
        _nth_weekday(year, 11, 3, 4) + timedelta(days=1),
        date(year, 12, 24),
        _observed(date(year, 12, 25)),
        date(year, 12, 31),
    }


def lead_timezones(lead: dict[str, Any]) -> list[str]:
    tz = (lead.get("timezone") or "").strip()
    if tz:
        try:
            ZoneInfo(tz)
            return [tz]
        except (ZoneInfoNotFoundError, ValueError):
            pass
    state = normalize_state(lead.get("state", ""))
    if state:
        return STATE_TIMEZONES[state]
    return phone_timezones(lead.get("phone", ""))


def primary_timezone(lead: dict[str, Any]) -> str:
    zones = lead_timezones(lead)
    return zones[0] if zones else "America/New_York"


def within_window(now_utc: datetime, zones: list[str], settings: dict[str, Any]) -> tuple[bool, str]:
    start = time.fromisoformat(settings["call_window_start"])
    end = time.fromisoformat(settings["call_window_end"])
    for zone in zones:
        local = now_utc.astimezone(ZoneInfo(zone))
        if local.weekday() not in settings["call_days"]:
            return False, f"not a calling day in {zone}"
        if settings.get("skip_us_holidays") and local.date() in us_holidays(local.year):
            return False, f"US holiday in {zone}"
        if not start <= local.time() < end:
            return False, f"outside calling hours in {zone} ({local:%H:%M})"
    return True, ""


def check_lead(
    db: Database,
    lead: dict[str, Any],
    settings: dict[str, Any],
    now_utc: datetime | None = None,
) -> CheckResult:
    now_utc = now_utc or utcnow()
    phone = lead.get("phone", "")
    if not phone.startswith("+1"):
        return CheckResult(False, "invalid phone number", permanent=True)
    if db.is_dnc(phone):
        return CheckResult(False, "number is on the do-not-call list", permanent=True)
    if settings.get("require_consent", True) and not lead.get("consent"):
        return CheckResult(False, "no recorded prior express consent", permanent=True)
    zones = lead_timezones(lead)
    if not zones:
        return CheckResult(False, "cannot determine the lead's time zone (set state or timezone)", permanent=True)
    ok, reason = within_window(now_utc, zones, settings)
    if not ok:
        return CheckResult(False, reason)
    per_day = int(settings.get("max_calls_per_phone_per_day", 1))
    if db.calls_to_phone_since(phone, now_utc - timedelta(hours=24)) >= per_day:
        return CheckResult(False, "daily call limit for this number reached")
    return CheckResult(True)
