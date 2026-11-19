from __future__ import annotations

import phonenumbers
from phonenumbers import timezone as pn_timezone


def normalize_phone(raw: str, region: str = "US") -> str | None:
    """Return the E.164 form of a US/Canada (NANP) number, or None if invalid."""
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(str(raw).strip(), region)
    except phonenumbers.NumberParseException:
        return None
    if parsed.country_code != 1 or not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_any_phone(raw: str, default_region: str = "US") -> str | None:
    """Return E.164 for any valid country. Prefer +countrycode; otherwise assume `default_region`."""
    if not raw:
        return None
    text = str(raw).strip()
    region = None if text.startswith("+") else default_region
    try:
        parsed = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def phone_timezones(e164: str) -> list[str]:
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return []
    zones = [z for z in pn_timezone.time_zones_for_number(parsed) if z and z != "Etc/Unknown"]
    return zones


def pretty_phone(e164: str) -> str:
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return e164
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
