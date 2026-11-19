"""CSV import for leads and do-not-call lists."""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from .compliance import normalize_state
from .db import Database
from .phones import normalize_phone

ALIASES: dict[str, tuple[str, ...]] = {
    "phone": ("phone", "phone_number", "phonenumber", "number", "mobile", "telephone", "tel", "business_phone"),
    "business_name": ("business_name", "business", "company", "company_name", "name_of_business", "organization"),
    "contact_name": ("contact_name", "contact", "name", "owner", "owner_name", "full_name", "first_name"),
    "email": ("email", "email_address", "e-mail"),
    "website": ("website", "url", "site", "web", "domain", "website_url"),
    "industry": ("industry", "category", "niche", "type", "business_type"),
    "city": ("city", "town"),
    "state": ("state", "region", "province", "st"),
    "timezone": ("timezone", "time_zone", "tz"),
    "consent": ("consent", "opt_in", "optin", "consented", "has_consent"),
    "consent_source": ("consent_source", "opt_in_source", "consent_proof", "source"),
    "notes": ("notes", "note", "comments", "description"),
}
TRUTHY = {"1", "true", "yes", "y", "x", "opted_in", "opt-in", "written"}


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9_\-]", "", header.strip().lower().replace(" ", "_"))


def _map_headers(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    normalized = {_key(h): h for h in headers if h}
    for field, aliases in ALIASES.items():
        for alias in aliases:
            if alias in normalized and normalized[alias] not in mapping.values():
                mapping[field] = normalized[alias]
                break
    return mapping


def import_leads_csv(db: Database, text: str, campaign_id: int | None, default_consent: bool = False) -> dict[str, Any]:
    text = text.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    mapping = _map_headers(reader.fieldnames or [])
    if "phone" not in mapping:
        return {"imported": 0, "updated": 0, "skipped": [{"row": 1, "reason": "no phone column found"}]}

    imported = updated = 0
    skipped: list[dict[str, Any]] = []
    for idx, row in enumerate(reader, start=2):
        values = {f: (row.get(col) or "").strip() for f, col in mapping.items()}
        phone = normalize_phone(values.pop("phone", ""))
        if phone is None:
            skipped.append({"row": idx, "reason": "invalid US phone number"})
            continue
        if "state" in values:
            values["state"] = normalize_state(values["state"]) or values["state"][:2].upper()
        consent_raw = values.pop("consent", "")
        values["consent"] = (consent_raw.lower() in TRUTHY) if consent_raw else default_consent
        if values["consent"] and not values.get("consent_source"):
            values["consent_source"] = "csv import" if consent_raw else "bulk: confirmed by operator at import"
        values = {k: v for k, v in values.items() if v not in ("", None) or k == "consent"}

        existing = db.find_lead(campaign_id, phone)
        if existing:
            db.update_lead(existing["id"], **values)
            updated += 1
        else:
            lead = db.create_lead(phone, campaign_id=campaign_id, **values)
            if db.is_dnc(phone):
                db.update_lead(lead["id"], status="do_not_call", status_reason="on do-not-call list")
            imported += 1
    return {"imported": imported, "updated": updated, "skipped": skipped[:200], "skipped_count": len(skipped)}


def _digits_to_e164(digits: str) -> str | None:
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        return None
    return normalize_phone("+" + digits)


def parse_phone_list(text: str) -> list[str]:
    """Parse one number per line; also accepts the registry's "areacode,number" lines."""
    phones = []
    for line in text.splitlines():
        whole = _digits_to_e164(re.sub(r"\D", "", line))
        if whole:
            phones.append(whole)
            continue
        for token in re.split(r"[\s,;|]+", line):
            number = _digits_to_e164(re.sub(r"\D", "", token))
            if number:
                phones.append(number)
    return phones
