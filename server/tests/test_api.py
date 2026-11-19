from fastapi.testclient import TestClient

from agentvoice.api import create_app
from agentvoice.twilio import compute_signature, validate_signature

AUTH = {"Authorization": "Bearer secret-token"}


def client_for(settings, db):
    return TestClient(create_app(settings, db))


def test_twilio_signature_matches_documented_example():
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+12349013030",
        "Digits": "1234",
        "From": "+12349013030",
        "To": "+18005551212",
    }
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    assert compute_signature("12345", url, params) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    assert not validate_signature("12345", url, params, "bogus")


def test_auth_required(settings, db):
    with client_for(settings, db) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
        body = client.get("/api/status", headers=AUTH).json()
        assert body["twilio_configured"] and body["openai_configured"]
        assert body["voice_backend"] == "openai" and body["voice_available"] is True


def test_status_reports_ollama_when_openai_key_missing(make_settings, db):
    settings = make_settings(openai_api_key="", twilio_account_sid="", voice_backend="auto")
    with client_for(settings, db) as client:
        body = client.get("/api/status", headers=AUTH).json()
        assert body["voice_backend"] == "ollama"
        assert body["voice_available"] is True
        assert body["ollama_model"] == "llama3.2:1b"
        assert body["openai_configured"] is False
        assert body["twilio_configured"] is False


def test_campaign_lead_and_settings_flow(settings, db):
    with client_for(settings, db) as client:
        campaign = client.post("/api/campaigns", json={"name": "Dallas contractors"}, headers=AUTH).json()
        csv_text = "business,phone,state,consent\nAce Roofing,214-555-0101,TX,yes\nNo Consent LLC,214-555-0102,TX,no\n"
        result = client.post(f"/api/leads/import?campaign_id={campaign['id']}", content=csv_text, headers=AUTH).json()
        assert result["imported"] == 2

        leads = client.get(f"/api/leads?campaign_id={campaign['id']}", headers=AUTH).json()
        assert leads["total"] == 2
        no_consent = next(l for l in leads["items"] if l["business_name"] == "No Consent LLC")
        detail = client.get(f"/api/leads/{no_consent['id']}", headers=AUTH).json()
        assert detail["callable_now"] is False

        dup = client.post("/api/leads", json={"phone": "2145550101", "campaign_id": campaign["id"]}, headers=AUTH)
        assert dup.status_code == 409
        assert client.post("/api/leads", json={"phone": "12"}, headers=AUTH).status_code == 422

        assert client.post("/api/dnc", json={"phone": "214-555-0101"}, headers=AUTH).status_code == 201
        ace = client.get("/api/leads?q=Ace", headers=AUTH).json()["items"][0]
        assert ace["status"] == "do_not_call"

        bad = client.put("/api/settings/agent", json={"call_window_end": "22:00"}, headers=AUTH)
        assert bad.status_code == 422
        good = client.put("/api/settings/agent", json={"voice": "cedar"}, headers=AUTH).json()
        assert good["voice"] == "cedar"

        pb = client.put("/api/settings/playbook", json={"company_name": "Acme Web"}, headers=AUTH).json()
        assert pb["company_name"] == "Acme Web"
        assert client.put("/api/settings/playbook", json={"disclosure": " "}, headers=AUTH).status_code == 422
        preview = client.get("/api/prompt/preview", headers=AUTH).json()
        assert "Acme Web" in preview["instructions"]

        started = client.post(f"/api/campaigns/{campaign['id']}/start", headers=AUTH).json()
        assert started["status"] == "running"

        renamed = client.post(
            f"/api/campaigns/{campaign['id']}",
            json={"name": "Dallas roofers"},
            headers={**AUTH, "X-HTTP-Method-Override": "PATCH"},
        ).json()
        assert renamed["name"] == "Dallas roofers"


def test_twilio_voice_webhook_requires_valid_signature(settings, db):
    call = db.create_call(phone="+12145550101", lead_id=None, campaign_id=None)
    path = f"/twilio/voice?call_id={call['id']}"
    params = {"CallSid": "CA999", "CallStatus": "in-progress"}
    with client_for(settings, db) as client:
        assert client.post(path, data=params).status_code == 403
        sig = compute_signature(settings.twilio_auth_token, settings.public_base_url + path, params)
        resp = client.post(path, data=params, headers={"X-Twilio-Signature": sig})
        assert resp.status_code == 200
        assert "<Connect><Stream url=\"wss://agent.example.com/twilio/media\">" in resp.text
        assert call["stream_token"] in resp.text
    assert db.get_call(call["id"])["twilio_sid"] == "CA999"


def test_status_callback_schedules_retry(settings, db):
    campaign = db.create_campaign("c", max_attempts=3, retry_minutes=120)
    lead = db.create_lead("+12145550101", campaign_id=campaign["id"], state="TX", consent=True)
    db.mark_lead_dialing(lead["id"])
    call = db.create_call(phone=lead["phone"], lead_id=lead["id"], campaign_id=campaign["id"])
    path = f"/twilio/status?call_id={call['id']}"
    params = {"CallSid": "CA1", "CallStatus": "no-answer"}
    sig = compute_signature(settings.twilio_auth_token, settings.public_base_url + path, params)
    with client_for(settings, db) as client:
        assert client.post(path, data=params, headers={"X-Twilio-Signature": sig}).status_code == 204
    lead = db.get_lead(lead["id"])
    assert lead["status"] == "retry" and lead["next_attempt_at"]
    assert db.get_call(call["id"])["outcome"] == "no_answer"


def test_dashboard_served(settings, db):
    with client_for(settings, db) as client:
        resp = client.get("/")
        assert resp.status_code == 200 and "AgentVoice" in resp.text
