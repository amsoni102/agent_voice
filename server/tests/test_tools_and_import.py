import json
from datetime import datetime, timezone

from agentvoice.agent_settings import DEFAULT_AGENT_SETTINGS
from agentvoice.importer import import_leads_csv, parse_phone_list
from agentvoice.playbook import DEFAULT_PLAYBOOK, build_instructions
from agentvoice.research import analyze_html
from agentvoice.tools import ToolRunner, open_slots, spoken_time

MONDAY_9UTC = datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)


class Host:
    def __init__(self):
        self.hangup = None
        self.transfer = None

    def request_hangup(self, reason):
        self.hangup = reason

    async def request_transfer(self, number, reason):
        self.transfer = number


def test_csv_import_maps_headers_and_dedupes(db):
    campaign = db.create_campaign("Austin restaurants")
    csv_text = (
        "Company,Phone Number,Owner,Website,State,Consent,Notes\n"
        "Joe's Tacos,(512) 555-0142,Joe Ruiz,joestacos.com,Texas,yes,family run\n"
        "Bad Row,123,,,TX,yes,\n"
        "Joe's Tacos,512-555-0142,Joe R.,joestacos.com,TX,yes,dupe\n"
    )
    result = import_leads_csv(db, csv_text, campaign["id"])
    assert result["imported"] == 1 and result["updated"] == 1 and result["skipped_count"] == 1
    lead = db.find_lead(campaign["id"], "+15125550142")
    assert lead["state"] == "TX" and lead["consent"] is True and lead["contact_name"] == "Joe R."


def test_dnc_parse_formats():
    phones = parse_phone_list("2125550123\n212,5550124\n+1 (212) 555-0125\njunk\n")
    assert phones == ["+12125550123", "+12125550124", "+12125550125"]


def test_open_slots_respect_notice_and_bookings(db):
    slots = open_slots(db, DEFAULT_AGENT_SETTINGS, "America/Chicago", now_utc=MONDAY_9UTC)
    assert slots, "expected open slots"
    assert all(s.tzinfo is not None for s in slots)
    first = slots[0]
    # Seller hours 10:00-17:00 Eastern; 3h notice from 09:00 Eastern -> earliest 12:00 Eastern = 11:00 Central.
    assert first.hour * 60 + first.minute >= 11 * 60
    db.create_meeting(starts_at=first.astimezone(timezone.utc).isoformat(), duration_minutes=20, lead_id=None)
    again = open_slots(db, DEFAULT_AGENT_SETTINGS, "America/Chicago", now_utc=MONDAY_9UTC)
    assert first not in again


async def test_booking_flow_updates_lead(db):
    lead = db.create_lead("+15125550142", business_name="Joe's Tacos", state="TX", consent=True)
    call = db.create_call(phone=lead["phone"], lead_id=lead["id"], campaign_id=None)
    runner = ToolRunner(
        db=db, host=Host(), call_id=call["id"], lead=lead,
        agent_settings=DEFAULT_AGENT_SETTINGS, playbook=DEFAULT_PLAYBOOK,
    )
    avail = await runner.run("check_availability", "{}")
    assert avail["ok"] and avail["slots"]
    slot = avail["slots"][0]
    bad = await runner.run(
        "book_meeting",
        json.dumps({"date": slot["date"], "time": slot["time"], "contact_name": "Joe", "email": "not-an-email"}),
    )
    assert not bad["ok"]
    booked = await runner.run(
        "book_meeting",
        json.dumps({"date": slot["date"], "time": slot["time"], "contact_name": "Joe Ruiz", "email": "joe@joestacos.com"}),
    )
    assert booked["ok"], booked
    lead = db.get_lead(lead["id"])
    assert lead["status"] == "meeting_booked" and lead["email"] == "joe@joestacos.com"
    assert db.get_call(call["id"])["outcome"] == "meeting_booked"
    # Same slot can't be double-booked.
    again = await runner.run(
        "book_meeting",
        json.dumps({"date": slot["date"], "time": slot["time"], "contact_name": "X", "email": "x@y.com"}),
    )
    assert not again["ok"]


async def test_dnc_tool_and_end_call(db):
    lead = db.create_lead("+15125550143", state="TX", consent=True)
    call = db.create_call(phone=lead["phone"], lead_id=lead["id"], campaign_id=None)
    host = Host()
    runner = ToolRunner(db=db, host=host, call_id=call["id"], lead=lead,
                        agent_settings=DEFAULT_AGENT_SETTINGS, playbook=DEFAULT_PLAYBOOK)
    assert (await runner.run("add_to_do_not_call", '{"reason":"asked"}'))["ok"]
    assert db.is_dnc("+15125550143")
    assert db.get_lead(lead["id"])["status"] == "do_not_call"
    await runner.run("end_call", '{"reason":"opt out"}')
    assert host.hangup == "opt out"
    assert not (await runner.run("transfer_to_human", "{}"))["ok"]


def test_instructions_contain_disclosure_and_lead_context(db):
    lead = db.create_lead("+15125550142", business_name="Joe's Tacos", state="TX", industry="restaurant")
    lead = db.get_lead(lead["id"])
    lead["research"] = {"talking_points": ["Their site doesn't look mobile-friendly."]}
    text = build_instructions(DEFAULT_PLAYBOOK, DEFAULT_AGENT_SETTINGS, lead, now_utc=MONDAY_9UTC)
    assert "Disclosure line" not in text and "recorded" not in text
    with_line = build_instructions({**DEFAULT_PLAYBOOK, "disclosure": "this call is recorded."}, DEFAULT_AGENT_SETTINGS, lead, now_utc=MONDAY_9UTC)
    assert 'Disclosure line: "this call is recorded."' in with_line
    assert "Joe's Tacos" in text and "mobile-friendly" in text
    assert "America/Chicago" in text
    assert "never deny it" in text and "Never claim to be human" in text


def test_voice_instructions_carry_company_memory(db):
    from agentvoice.playbook import END_MARKER, build_voice_instructions

    text = build_voice_instructions(DEFAULT_PLAYBOOK, DEFAULT_AGENT_SETTINGS, None, direction="practice", now_utc=MONDAY_9UTC)
    assert "Delaware" in text and "East Coast" in text and "work with anyone" in text
    assert "Stellar Technologies team" in text and "never deny it" in text
    assert END_MARKER in text and "{company_name}" not in text


def test_spoken_time():
    from zoneinfo import ZoneInfo

    dt = datetime(2026, 11, 17, 14, 30, tzinfo=ZoneInfo("America/Chicago"))
    assert spoken_time(dt) == "Tuesday, November 17 at 2:30 PM Central time"


def test_research_analysis():
    html = "<html><head><title>Joe's Tacos</title></head><body>© 2016 Joe's</body></html>"
    result = analyze_html(html, "http://joestacos.com/", 4200, 200)
    text = " ".join(result["talking_points"])
    assert "not secure" in text and "mobile-friendly" in text and "2016" in text and "slow" in text
