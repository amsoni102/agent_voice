"""Lightweight pre-call research on a prospect's website to give the agent a relevant hook."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .db import now_iso, parse_iso

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_VIEWPORT = re.compile(r"<meta[^>]+name=[\"']viewport[\"']", re.I)
_COPYRIGHT = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", re.I)
_BOOKING = re.compile(r"book (?:now|online|an appointment)|schedule (?:now|online)|calendly|acuityscheduling|squareup\.com/appointments", re.I)
_ECOM = re.compile(r"add to cart|shopify|woocommerce|checkout", re.I)
_BUILDERS = {
    "wix": re.compile(r"wix\.com|_wixCIDX|wixstatic", re.I),
    "squarespace": re.compile(r"squarespace", re.I),
    "godaddy builder": re.compile(r"godaddy|img1\.wsimg\.com", re.I),
    "wordpress": re.compile(r"wp-content|wordpress", re.I),
    "weebly": re.compile(r"weebly", re.I),
}

RESEARCH_TTL = timedelta(days=7)


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def analyze_html(html: str, final_url: str, elapsed_ms: int, status: int) -> dict[str, Any]:
    title_match = _TITLE.search(html)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip()[:120] if title_match else ""
    years = [int(y) for y in _COPYRIGHT.findall(html) if 1995 <= int(y) <= datetime.now().year + 1]
    copyright_year = max(years) if years else None
    builder = next((name for name, rx in _BUILDERS.items() if rx.search(html)), "")
    mobile = bool(_VIEWPORT.search(html))
    https = final_url.lower().startswith("https://")

    points: list[str] = []
    if status >= 400:
        points.append(f"Their website returned an error (HTTP {status}); it may be down or broken.")
    if not https:
        points.append("Their site is not secure (no HTTPS), so browsers show a 'Not secure' warning.")
    if not mobile:
        points.append("Their site doesn't look mobile-friendly (no responsive viewport), so it's hard to use on phones.")
    if elapsed_ms > 3500:
        points.append(f"Their homepage is slow (took about {elapsed_ms / 1000:.0f} seconds to load).")
    if copyright_year and copyright_year <= datetime.now().year - 3:
        points.append(f"The footer says copyright {copyright_year}, so the site may not have been updated in years.")
    if not _BOOKING.search(html):
        points.append("No online booking or scheduling found on the homepage.")
    if _ECOM.search(html):
        points.append("They appear to sell online already (store/cart detected).")
    if builder in ("wix", "godaddy builder", "weebly"):
        points.append(f"The site seems built with a DIY builder ({builder}).")
    if not points:
        points.append("Their website looks reasonably modern; focus on growth (more leads, bookings, an app).")

    return {
        "checked_at": now_iso(),
        "final_url": final_url,
        "status": status,
        "https": https,
        "mobile_friendly": mobile,
        "load_ms": elapsed_ms,
        "title": title,
        "copyright_year": copyright_year,
        "builder": builder,
        "talking_points": points,
    }


async def research_website(url: str, timeout: float = 8.0) -> dict[str, Any]:
    url = _normalize_url(url)
    if not url:
        return {
            "checked_at": now_iso(),
            "talking_points": ["We have no website on file for them; they may not have one at all."],
        }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
            try:
                resp = await client.get(url)
            except httpx.ConnectError:
                if url.startswith("https://"):
                    resp = await client.get("http://" + url[len("https://"):])
                else:
                    raise
            elapsed = int((time.monotonic() - started) * 1000)
            return analyze_html(resp.text[:400_000], str(resp.url), elapsed, resp.status_code)
    except (httpx.HTTPError, UnicodeDecodeError) as exc:
        return {
            "checked_at": now_iso(),
            "final_url": url,
            "error": type(exc).__name__,
            "talking_points": [
                "Their website on file didn't load when we checked; it may be down or the domain expired."
            ],
        }


def research_is_fresh(research: dict[str, Any]) -> bool:
    checked = parse_iso(research.get("checked_at")) if research else None
    return bool(checked and datetime.now(timezone.utc) - checked < RESEARCH_TTL)
