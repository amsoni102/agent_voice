from __future__ import annotations

import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .agent_settings import SettingsError, get_agent_settings, save_agent_settings
from .calls import CallError, CallManager
from .compliance import check_lead, normalize_state, primary_timezone
from .config import Settings, load_settings
from .db import LEAD_STATUSES, Database, utcnow
from .importer import import_leads_csv, parse_phone_list
from .phones import normalize_phone
from .playbook import DEFAULT_PLAYBOOK, build_instructions, get_playbook, save_playbook
from .realtime import VoiceSession
from .transports import BrowserTransport, TwilioTransport
from .twilio import reject_twiml, stream_twiml, validate_signature

log = logging.getLogger("agentvoice.api")
WEB_DIR = Path(__file__).parent / "web"


def resolve_admin_token(settings: Settings, db: Database) -> str:
    if settings.admin_token:
        return settings.admin_token
    token = db.get_setting("generated_admin_token")
    if not token:
        token = secrets.token_urlsafe(24)
        db.set_setting("generated_admin_token", token)
    return token


# ----------------------------------------------------------------- schemas
class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    extra_instructions: str = ""
    max_concurrent: int = Field(1, ge=1, le=20)
    max_attempts: int = Field(3, ge=1, le=6)
    retry_minutes: int = Field(1440, ge=60, le=20160)


class CampaignPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    extra_instructions: str | None = None
    max_concurrent: int | None = Field(None, ge=1, le=20)
    max_attempts: int | None = Field(None, ge=1, le=6)
    retry_minutes: int | None = Field(None, ge=60, le=20160)


class LeadIn(BaseModel):
    phone: str
    campaign_id: int | None = None
    business_name: str = ""
    contact_name: str = ""
    email: str = ""
    website: str = ""
    industry: str = ""
    city: str = ""
    state: str = ""
    timezone: str = ""
    consent: bool = False
    consent_source: str = ""
    notes: str = ""


class LeadPatch(BaseModel):
    phone: str | None = None
    campaign_id: int | None = None
    business_name: str | None = None
    contact_name: str | None = None
    email: str | None = None
    website: str | None = None
    industry: str | None = None
    city: str | None = None
    state: str | None = None
    timezone: str | None = None
    consent: bool | None = None
    consent_source: str | None = None
    notes: str | None = None
    status: Literal[LEAD_STATUSES] | None = None  # type: ignore[valid-type]


class DncIn(BaseModel):
    phone: str
    reason: str = ""


class TestCallIn(BaseModel):
    phone: str | None = None


class MeetingPatch(BaseModel):
    status: Literal["booked", "completed", "no_show", "cancelled"]


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = db or Database(settings.db_path)
    manager = CallManager(settings, db)
    admin_token = resolve_admin_token(settings, db)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        manager.start()
        log.info("AgentVoice %s ready. Twilio=%s OpenAI=%s", __version__, settings.twilio_configured, settings.openai_configured)
        yield
        await manager.shutdown()

    app = FastAPI(title="AgentVoice", version=__version__, lifespan=lifespan)
    app.state.db = db
    app.state.manager = manager
    app.state.settings = settings
    app.state.admin_token = admin_token

    def check_token(value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value, admin_token)  # type: ignore[arg-type]

    def require_auth(request: Request) -> None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if not check_token(token):
            raise HTTPException(401, "invalid or missing API token")

    async def twilio_form(request: Request) -> dict[str, str]:
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        if settings.validate_twilio_signature:
            url = settings.public_base_url + request.url.path
            if request.url.query:
                url += "?" + request.url.query
            if not validate_signature(
                settings.twilio_auth_token, url, params, request.headers.get("x-twilio-signature", "")
            ):
                raise HTTPException(403, "invalid Twilio signature")
        return params

    def lead_or_404(lead_id: int) -> dict[str, Any]:
        lead = db.get_lead(lead_id)
        if lead is None:
            raise HTTPException(404, "lead not found")
        return lead

    def campaign_or_404(campaign_id: int) -> dict[str, Any]:
        campaign = db.get_campaign(campaign_id)
        if campaign is None:
            raise HTTPException(404, "campaign not found")
        return campaign

    def clean_lead_fields(data: dict[str, Any]) -> dict[str, Any]:
        if "phone" in data and data["phone"] is not None:
            phone = normalize_phone(data["phone"])
            if phone is None:
                raise HTTPException(422, "invalid US phone number")
            data["phone"] = phone
        if data.get("state"):
            data["state"] = normalize_state(data["state"]) or data["state"]
        if data.get("timezone"):
            try:
                ZoneInfo(data["timezone"])
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(422, "timezone must be an IANA zone like America/Chicago") from exc
        if data.get("campaign_id") is not None:
            campaign_or_404(data["campaign_id"])
        return data

    auth = [Depends(require_auth)]

    # ---------------------------------------------------------------- status
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/api/status", dependencies=auth)
    async def status() -> dict[str, Any]:
        today = utcnow().astimezone(ZoneInfo("America/New_York")).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "version": __version__,
            "twilio_configured": settings.twilio_configured,
            "openai_configured": settings.openai_configured,
            "dialer_enabled": settings.dialer_enabled,
            "public_base_url": settings.public_base_url,
            "from_number": settings.twilio_from_number,
            "running_campaigns": [c["id"] for c in db.running_campaigns()],
            "today": db.stats(today),
            "last_7_days": db.stats(today - timedelta(days=7)),
        }

    # ------------------------------------------------------------- campaigns
    @app.get("/api/campaigns", dependencies=auth)
    async def list_campaigns() -> list[dict[str, Any]]:
        return db.list_campaigns()

    @app.post("/api/campaigns", dependencies=auth, status_code=201)
    async def create_campaign(body: CampaignIn) -> dict[str, Any]:
        data = body.model_dump()
        return db.create_campaign(data.pop("name"), **data)

    @app.patch("/api/campaigns/{campaign_id}", dependencies=auth)
    async def patch_campaign(campaign_id: int, body: CampaignPatch) -> dict[str, Any]:
        campaign_or_404(campaign_id)
        return db.update_campaign(campaign_id, **body.model_dump(exclude_none=True))  # type: ignore[return-value]

    @app.post("/api/campaigns/{campaign_id}/start", dependencies=auth)
    async def start_campaign(campaign_id: int) -> dict[str, Any]:
        campaign_or_404(campaign_id)
        if not settings.twilio_configured or not settings.openai_configured:
            raise HTTPException(409, "configure Twilio and OpenAI in .env before starting a campaign")
        return db.update_campaign(campaign_id, status="running")  # type: ignore[return-value]

    @app.post("/api/campaigns/{campaign_id}/pause", dependencies=auth)
    async def pause_campaign(campaign_id: int) -> dict[str, Any]:
        campaign_or_404(campaign_id)
        return db.update_campaign(campaign_id, status="paused")  # type: ignore[return-value]

    @app.delete("/api/campaigns/{campaign_id}", dependencies=auth, status_code=204)
    async def delete_campaign(campaign_id: int) -> Response:
        campaign_or_404(campaign_id)
        db.delete_campaign(campaign_id)
        return Response(status_code=204)

    # ----------------------------------------------------------------- leads
    @app.get("/api/leads", dependencies=auth)
    async def list_leads(
        campaign_id: int | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        rows, total = db.list_leads(campaign_id, status, q, limit, offset)
        return {"items": rows, "total": total}

    @app.post("/api/leads", dependencies=auth, status_code=201)
    async def create_lead(body: LeadIn) -> dict[str, Any]:
        data = clean_lead_fields(body.model_dump())
        if db.find_lead(data.get("campaign_id"), data["phone"]):
            raise HTTPException(409, "this phone number is already in that campaign")
        lead = db.create_lead(data.pop("phone"), **data)
        if db.is_dnc(lead["phone"]):
            lead = db.update_lead(lead["id"], status="do_not_call", status_reason="on do-not-call list")  # type: ignore[assignment]
        return lead

    @app.post("/api/leads/import", dependencies=auth)
    async def import_leads(
        request: Request,
        campaign_id: int | None = None,
        consent_confirmed: bool = False,
    ) -> dict[str, Any]:
        if campaign_id is not None:
            campaign_or_404(campaign_id)
        raw = await request.body()
        if len(raw) > 20 * 1024 * 1024:
            raise HTTPException(413, "file too large (20 MB max)")
        text = raw.decode("utf-8", errors="replace")
        return import_leads_csv(db, text, campaign_id, default_consent=consent_confirmed)

    @app.get("/api/leads/{lead_id}", dependencies=auth)
    async def get_lead(lead_id: int) -> dict[str, Any]:
        lead = lead_or_404(lead_id)
        agent = get_agent_settings(db)
        check = check_lead(db, lead, agent)
        lead["callable_now"] = check.ok
        lead["callable_reason"] = check.reason
        lead["local_timezone"] = primary_timezone(lead)
        lead["calls"] = db.list_calls(lead_id=lead_id, limit=50)
        lead["dnc"] = db.is_dnc(lead["phone"])
        return lead

    @app.patch("/api/leads/{lead_id}", dependencies=auth)
    async def patch_lead(lead_id: int, body: LeadPatch) -> dict[str, Any]:
        lead_or_404(lead_id)
        data = clean_lead_fields(body.model_dump(exclude_unset=True))
        if data.get("status") in ("new", "retry", "callback") and lead_or_404(lead_id)["status"] == "do_not_call":
            raise HTTPException(409, "remove the number from the do-not-call list first")
        return db.update_lead(lead_id, **data)  # type: ignore[return-value]

    @app.delete("/api/leads/{lead_id}", dependencies=auth, status_code=204)
    async def delete_lead(lead_id: int) -> Response:
        lead_or_404(lead_id)
        db.delete_lead(lead_id)
        return Response(status_code=204)

    @app.post("/api/leads/{lead_id}/call", dependencies=auth)
    async def call_lead(lead_id: int) -> dict[str, Any]:
        lead = lead_or_404(lead_id)
        try:
            return await manager.place_call(lead, kind="manual")
        except CallError as exc:
            raise HTTPException(409, str(exc)) from exc

    # ----------------------------------------------------------------- calls
    @app.get("/api/calls", dependencies=auth)
    async def list_calls(
        lead_id: int | None = None,
        active: bool = False,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> list[dict[str, Any]]:
        return db.list_calls(lead_id=lead_id, limit=limit, offset=offset, active_only=active)

    @app.get("/api/calls/{call_id}", dependencies=auth)
    async def get_call(call_id: int) -> dict[str, Any]:
        call = db.get_call(call_id)
        if call is None:
            raise HTTPException(404, "call not found")
        call.pop("stream_token", None)
        call["transcript"] = db.get_transcript(call_id)
        call["live"] = call_id in manager.sessions
        call["lead"] = db.get_lead(call["lead_id"]) if call.get("lead_id") else None
        return call

    @app.post("/api/calls/{call_id}/hangup", dependencies=auth)
    async def hangup_call(call_id: int) -> dict[str, Any]:
        if db.get_call(call_id) is None:
            raise HTTPException(404, "call not found")
        await manager.hangup(call_id)
        return {"ok": True}

    @app.post("/api/test-call", dependencies=auth)
    async def test_call(body: TestCallIn) -> dict[str, Any]:
        agent = get_agent_settings(db)
        phone = normalize_phone(body.phone) if body.phone else agent["test_phone_number"]
        if not phone:
            raise HTTPException(422, "set test_phone_number in agent settings first")
        try:
            return await manager.place_test_call(phone)
        except CallError as exc:
            raise HTTPException(409, str(exc)) from exc

    # -------------------------------------------------------------- meetings
    @app.get("/api/meetings", dependencies=auth)
    async def list_meetings(upcoming: bool = False) -> list[dict[str, Any]]:
        return db.list_meetings(upcoming_only=upcoming)

    @app.patch("/api/meetings/{meeting_id}", dependencies=auth)
    async def patch_meeting(meeting_id: int, body: MeetingPatch) -> dict[str, Any]:
        meeting = db.update_meeting(meeting_id, body.status)
        if meeting is None:
            raise HTTPException(404, "meeting not found")
        return meeting

    # ------------------------------------------------------------------- dnc
    @app.get("/api/dnc", dependencies=auth)
    async def list_dnc(limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        rows, total = db.list_dnc(limit, offset)
        return {"items": rows, "total": total}

    @app.post("/api/dnc", dependencies=auth, status_code=201)
    async def add_dnc(body: DncIn) -> dict[str, Any]:
        phone = normalize_phone(body.phone)
        if phone is None:
            raise HTTPException(422, "invalid US phone number")
        db.add_dnc(phone, body.reason, source="manual")
        return {"ok": True, "phone": phone}

    @app.post("/api/dnc/import", dependencies=auth)
    async def import_dnc(request: Request, source: str = "national_registry") -> dict[str, Any]:
        text = (await request.body()).decode("utf-8", errors="replace")
        phones = parse_phone_list(text)
        added = db.add_dnc_many(phones, source=source[:40])
        return {"parsed": len(phones), "changes": added}

    @app.delete("/api/dnc/{phone}", dependencies=auth, status_code=204)
    async def remove_dnc(phone: str) -> Response:
        normalized = normalize_phone(phone)
        if normalized is None:
            raise HTTPException(422, "invalid US phone number")
        db.remove_dnc(normalized)
        return Response(status_code=204)

    # -------------------------------------------------------------- settings
    @app.get("/api/settings/agent", dependencies=auth)
    async def get_agent() -> dict[str, Any]:
        return get_agent_settings(db)

    @app.put("/api/settings/agent", dependencies=auth)
    async def put_agent(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return save_agent_settings(db, body)
        except (SettingsError, ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/settings/playbook", dependencies=auth)
    async def get_pb() -> dict[str, Any]:
        return get_playbook(db)

    @app.put("/api/settings/playbook", dependencies=auth)
    async def put_pb(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return save_playbook(db, body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/settings/playbook/reset", dependencies=auth)
    async def reset_pb() -> dict[str, Any]:
        db.set_setting("playbook", DEFAULT_PLAYBOOK)
        return get_playbook(db)

    @app.get("/api/prompt/preview", dependencies=auth)
    async def prompt_preview(lead_id: int | None = None) -> dict[str, Any]:
        lead = lead_or_404(lead_id) if lead_id else None
        campaign = db.get_campaign(lead["campaign_id"]) if lead and lead.get("campaign_id") else None
        text = build_instructions(get_playbook(db), get_agent_settings(db), lead, campaign)
        return {"instructions": text, "characters": len(text)}

    # ------------------------------------------------------- Twilio webhooks
    def stream_response(call: dict[str, Any]) -> Response:
        twiml = stream_twiml(
            f"{settings.public_ws_base}/twilio/media",
            {"call_id": str(call["id"]), "token": call["stream_token"]},
        )
        return Response(twiml, media_type="application/xml")

    @app.post("/twilio/voice")
    async def twilio_voice(request: Request, call_id: int) -> Response:
        params = await twilio_form(request)
        call = db.get_call(call_id)
        if call is None:
            return Response(reject_twiml(), media_type="application/xml")
        if params.get("CallSid") and not call.get("twilio_sid"):
            db.update_call(call_id, twilio_sid=params["CallSid"])
        db.update_call(call_id, status="in-progress", answered_at=call.get("answered_at") or utcnow().isoformat(timespec="seconds"))
        return stream_response(call)

    @app.post("/twilio/inbound")
    async def twilio_inbound(request: Request) -> Response:
        params = await twilio_form(request)
        if not settings.openai_configured:
            return Response(reject_twiml("Sorry, we can't take your call right now. Please try again later."), media_type="application/xml")
        phone = normalize_phone(params.get("From", "")) or params.get("From", "unknown")
        lead = db.find_lead_by_phone(phone)
        call = db.create_call(
            phone=phone,
            lead_id=lead["id"] if lead else None,
            campaign_id=lead.get("campaign_id") if lead else None,
            direction="inbound",
            kind="inbound",
        )
        db.update_call(call["id"], twilio_sid=params.get("CallSid"), status="in-progress", answered_at=utcnow().isoformat(timespec="seconds"))
        return stream_response(call)

    @app.post("/twilio/status")
    async def twilio_status(request: Request, call_id: int | None = None) -> Response:
        params = await twilio_form(request)
        if call_id is None and params.get("CallSid"):
            call = db.get_call_by_sid(params["CallSid"])
            call_id = call["id"] if call else None
        if call_id is not None:
            manager.on_status(call_id, params)
        return Response(status_code=204)

    @app.post("/twilio/amd")
    async def twilio_amd(request: Request, call_id: int) -> Response:
        params = await twilio_form(request)
        answered_by = params.get("AnsweredBy", "unknown")
        await manager.on_amd(call_id, answered_by)
        return Response(status_code=204)

    @app.websocket("/twilio/media")
    async def twilio_media(ws: WebSocket) -> None:
        await ws.accept()
        transport = TwilioTransport(ws, manager.twilio, settings.twilio_from_number)
        session: VoiceSession | None = None
        try:
            while True:
                data = json.loads(await ws.receive_text())
                event = data.get("event")
                if event == "media" and session is not None:
                    await session.feed_audio(data["media"]["payload"])
                elif event == "mark" and session is not None:
                    await session.on_mark(data.get("mark", {}).get("name", ""))
                elif event == "start":
                    start = data.get("start", {})
                    custom = start.get("customParameters", {}) or {}
                    try:
                        call = db.get_call(int(custom.get("call_id", "0")))
                    except ValueError:
                        call = None
                    if call is None or not hmac.compare_digest(str(custom.get("token", "")), call["stream_token"]):
                        log.warning("media stream rejected: bad call id/token")
                        await ws.close(code=1008)
                        return
                    transport.stream_sid = start.get("streamSid")
                    transport.call_sid = start.get("callSid") or call.get("twilio_sid")
                    lead = db.get_lead(call["lead_id"]) if call.get("lead_id") else None
                    campaign = db.get_campaign(call["campaign_id"]) if call.get("campaign_id") else None
                    session = VoiceSession(
                        settings=settings,
                        db=db,
                        call=call,
                        lead=lead,
                        campaign=campaign,
                        transport=transport,
                        direction="inbound" if call["direction"] == "inbound" else "outbound",
                        on_finished=manager.on_session_finished,
                    )
                    manager.sessions[call["id"]] = session
                    try:
                        await session.start()
                    except Exception as exc:  # noqa: BLE001
                        log.exception("call %s: could not start voice session", call["id"])
                        db.update_call(call["id"], error=f"voice session failed: {exc}"[:500])
                        await transport.hangup()
                        await session.stop("start failed")
                        return
                elif event == "stop":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if session is not None:
                await session.stop("stream ended")

    # ---------------------------------------------------------- practice mode
    @app.websocket("/ws/practice")
    async def practice(ws: WebSocket, token: str = "", lead_id: int | None = None) -> None:
        if not check_token(token):
            await ws.close(code=1008)
            return
        await ws.accept()
        if not settings.openai_configured:
            await ws.send_json({"type": "error", "message": "OPENAI_API_KEY is not configured on the server"})
            await ws.close()
            return
        lead = db.get_lead(lead_id) if lead_id else None
        campaign = db.get_campaign(lead["campaign_id"]) if lead and lead.get("campaign_id") else None
        call = db.create_call(phone=lead["phone"] if lead else "", lead_id=None, campaign_id=None, direction="outbound", kind="practice")
        db.update_call(call["id"], status="in-progress", answered_at=utcnow().isoformat(timespec="seconds"))
        transport = BrowserTransport(ws)
        session = VoiceSession(
            settings=settings,
            db=db,
            call=call,
            lead=lead,
            campaign=campaign,
            transport=transport,
            direction="practice",
            on_finished=manager.on_session_finished,
        )
        manager.sessions[call["id"]] = session
        try:
            await session.start()
            await ws.send_json({"type": "ready", "call_id": call["id"]})
            while True:
                data = await ws.receive_json()
                kind = data.get("type")
                if kind == "audio":
                    await session.feed_audio(data["audio"])
                elif kind == "mark":
                    await session.on_mark(data.get("name", ""))
                elif kind == "stop":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("practice session failed")
            try:
                await ws.send_json({"type": "error", "message": str(exc)})
            except Exception:  # noqa: BLE001
                pass
        finally:
            await session.stop("practice ended")

    @app.exception_handler(SettingsError)
    async def settings_error(_: Request, exc: SettingsError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
