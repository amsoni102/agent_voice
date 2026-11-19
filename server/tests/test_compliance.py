from datetime import date, datetime, timedelta, timezone

from agentvoice.agent_settings import DEFAULT_AGENT_SETTINGS, SettingsError, validate_agent_settings
from agentvoice.compliance import check_lead, lead_timezones, us_holidays

import pytest

# Tuesday 2026-11-17 15:00 UTC = 10:00 New York, 09:00 Chicago, 07:00 Los Angeles
TUESDAY_15UTC = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)


def lead(**kw):
    base = {"phone": "+12125550123", "state": "NY", "timezone": "", "consent": True}
    base.update(kw)
    return base


def test_ny_lead_callable_during_business_hours(db):
    assert check_lead(db, lead(), DEFAULT_AGENT_SETTINGS, TUESDAY_15UTC).ok


def test_california_too_early(db):
    result = check_lead(db, lead(phone="+14155550123", state="CA"), DEFAULT_AGENT_SETTINGS, TUESDAY_15UTC)
    assert not result.ok and not result.permanent
    assert "outside calling hours" in result.reason


def test_multi_timezone_state_requires_all_zones_open(db):
    # 14:30 UTC = 09:30 Eastern but 08:30 Central -> Florida spans both, must wait.
    at = datetime(2026, 11, 17, 14, 30, tzinfo=timezone.utc)
    assert not check_lead(db, lead(phone="+13055550123", state="FL"), DEFAULT_AGENT_SETTINGS, at).ok
    assert check_lead(db, lead(phone="+13055550123", timezone="America/New_York", state=""), DEFAULT_AGENT_SETTINGS, at).ok


def test_weekend_and_holiday_blocked(db):
    saturday = TUESDAY_15UTC + timedelta(days=4)
    assert "not a calling day" in check_lead(db, lead(), DEFAULT_AGENT_SETTINGS, saturday).reason
    thanksgiving = datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)
    assert date(2026, 11, 26) in us_holidays(2026)
    assert "holiday" in check_lead(db, lead(), DEFAULT_AGENT_SETTINGS, thanksgiving).reason


def test_consent_and_dnc_are_permanent_blocks(db):
    result = check_lead(db, lead(consent=False), DEFAULT_AGENT_SETTINGS, TUESDAY_15UTC)
    assert not result.ok and result.permanent
    relaxed = dict(DEFAULT_AGENT_SETTINGS, require_consent=False)
    assert check_lead(db, lead(consent=False), relaxed, TUESDAY_15UTC).ok

    db.add_dnc("+12125550123", "asked")
    result = check_lead(db, lead(), DEFAULT_AGENT_SETTINGS, TUESDAY_15UTC)
    assert not result.ok and result.permanent and "do-not-call" in result.reason


def test_daily_limit(db):
    always_open = dict(
        DEFAULT_AGENT_SETTINGS,
        call_window_start="00:00",
        call_window_end="23:59:59",
        call_days=list(range(7)),
        skip_us_holidays=False,
    )
    assert check_lead(db, lead(), always_open).ok
    db.create_call(phone="+12125550123", lead_id=None, campaign_id=None)
    result = check_lead(db, lead(), always_open)
    assert not result.ok and "daily call limit" in result.reason


def test_timezone_from_area_code():
    assert lead_timezones({"phone": "+13125550123"}) == ["America/Chicago"]


def test_settings_reject_window_outside_hard_limits():
    with pytest.raises(SettingsError):
        validate_agent_settings({"call_window_end": "21:00"})
    with pytest.raises(SettingsError):
        validate_agent_settings({"voice": "robot"})
    ok = validate_agent_settings({"test_phone_number": "(512) 555-0199"})
    assert ok["test_phone_number"] == "+15125550199"
    de = validate_agent_settings({"test_phone_number": "+49 170 1234567"})
    assert de["test_phone_number"].startswith("+49")
