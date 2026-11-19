"""Call orchestration: dialing, carrier status updates, retries and post-call summaries."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

import httpx

from .agent_settings import get_agent_settings
from .compliance import CheckResult, check_lead
from .config import Settings
from .db import Database, now_iso, parse_iso, to_iso, utcnow
from .gemini_llm import GeminiClient
from .ollama_llm import OllamaClient
from .research import research_is_fresh, research_website
from .twilio import TwilioClient, TwilioError

log = logging.getLogger("agentvoice.calls")

FINAL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled"}

SUMMARY_PROMPT = """You are a sales manager reviewing a phone call between our AI sales rep ("agent") and a
small-business prospect. We sell websites, online stores and mobile apps. Return JSON with:
- "summary": 2-4 sentences: who they are, their situation/needs, how the call went.
- "outcome": one of meeting_booked, interested, callback_scheduled, not_interested, do_not_call, wrong_number,
  gatekeeper, voicemail, no_conversation, other.
- "lead_score": one of hot, warm, cold, none.
- "next_step": one short sentence for the human sales team.
- "coaching": one short sentence on what the agent could do better next time."""


class CallError(RuntimeError):
    pass


class CallManager:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.twilio: TwilioClient | None = TwilioClient(settings) if settings.twilio_configured else None
        self.zadarma: Any = None
        self.whatsapp: Any = None
        if settings.zadarma_configured:
            from .zadarma import ZadarmaLine

            self.zadarma = ZadarmaLine(self)
        if settings.whatsapp_configured:
            from .whatsapp import WhatsAppLine

            self.whatsapp = WhatsAppLine(self)
        self.sessions: dict[int, Any] = {}
        self._dialer_task: asyncio.Task[Any] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._dial_lock = asyncio.Lock()
        self._last_dial = 0.0

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.zadarma is not None:
            self.spawn(self._start_zadarma())
        if self.settings.dialer_enabled and self._dialer_task is None:
            self._dialer_task = asyncio.create_task(self._dialer_loop(), name="dialer")

    async def _start_zadarma(self) -> None:
        try:
            await self.zadarma.start()
        except Exception as exc:  # noqa: BLE001
            self.zadarma.ua.last_error = f"SIP start failed: {exc}"
            log.exception("Zadarma SIP could not start")

    async def shutdown(self) -> None:
        if self._dialer_task:
            self._dialer_task.cancel()
        for session in list(self.sessions.values()):
            await session.stop("server shutdown")
        if self.zadarma is not None:
            await self.zadarma.stop()
        if self.whatsapp is not None:
            await self.whatsapp.stop()
        if self.twilio:
            await self.twilio.aclose()

    def start_line_session(self, call: dict[str, Any], transport: Any, direction: str) -> Any:
        """Voice session for a carrier line (Zadarma SIP / WhatsApp) that delivers audio through `transport`."""
        from .local_voice import create_voice_session

        lead = self.db.get_lead(call["lead_id"]) if call.get("lead_id") else None
        campaign = self.db.get_campaign(call["campaign_id"]) if call.get("campaign_id") else None
        session = create_voice_session(
            settings=self.settings, db=self.db, call=call, lead=lead, campaign=campaign,
            transport=transport, direction=direction, on_finished=self.on_session_finished,
        )
        transport.session = session
        self.sessions[call["id"]] = session
        return session

    def spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------ dialing
    async def _dialer_loop(self) -> None:
        log.info("dialer started")
        while True:
            try:
                self.db.expire_stale_calls(timedelta(hours=1))
                await self.dial_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("dialer tick failed")
            await asyncio.sleep(5)

    async def dial_tick(self) -> int:
        if not self.settings.phone_line or not self.settings.voice_available:
            return 0
        agent = get_agent_settings(self.db)
        loop = asyncio.get_running_loop()
        placed = 0
        for campaign in self.db.running_campaigns():
            slots = int(campaign["max_concurrent"]) - self.db.count_active_calls(campaign["id"])
            if slots <= 0:
                continue
            leads = self.db.dialable_leads(campaign, limit=max(20, slots * 10))
            if not leads and self.db.count_active_calls(campaign["id"]) == 0:
                pending = self.db.list_leads(campaign_id=campaign["id"], status="callback", limit=1)[1]
                retry = self.db.list_leads(campaign_id=campaign["id"], status="retry", limit=1)[1]
                new = self.db.list_leads(campaign_id=campaign["id"], status="new", limit=1)[1]
                if pending + retry + new == 0:
                    self.db.update_campaign(campaign["id"], status="finished")
                    log.info("campaign %s finished", campaign["id"])
                continue
            for lead in leads:
                if slots <= 0:
                    break
                if loop.time() - self._last_dial < agent["dial_interval_seconds"]:
                    return placed
                result = check_lead(self.db, lead, agent)
                if not result.ok:
                    if result.permanent:
                        self.db.update_lead(lead["id"], status="blocked", status_reason=result.reason)
                    continue
                try:
                    await self.place_call(lead, kind="campaign", agent=agent, preflight=result)
                    placed += 1
                    slots -= 1
                except CallError as exc:
                    log.warning("lead %s: %s", lead["id"], exc)
        return placed

    async def place_call(
        self,
        lead: dict[str, Any],
        *,
        kind: str = "manual",
        agent: dict[str, Any] | None = None,
        preflight: CheckResult | None = None,
    ) -> dict[str, Any]:
        if not self.settings.phone_line:
            raise CallError("No phone line configured: set ZADARMA_SIP_LOGIN/ZADARMA_SIP_PASSWORD or Twilio in .env")
        if not self.settings.voice_available:
            raise CallError("No voice backend (set GEMINI_API_KEY, start Ollama, or set OPENAI_API_KEY)")
        agent = agent or get_agent_settings(self.db)
        result = preflight or check_lead(self.db, lead, agent)
        if not result.ok:
            raise CallError(f"cannot call now: {result.reason}")

        async with self._dial_lock:
            if not research_is_fresh(lead.get("research") or {}):
                research = await research_website(lead.get("website", ""))
                self.db.merge_lead_json(lead["id"], "research", research)

            campaign_id = lead.get("campaign_id")
            call = self.db.create_call(phone=lead["phone"], lead_id=lead["id"], campaign_id=campaign_id, kind=kind)
            self.db.mark_lead_dialing(lead["id"])
            self._last_dial = asyncio.get_running_loop().time()
            return await self._dial(call, lead["phone"], agent)

    async def place_test_call(self, phone: str) -> dict[str, Any]:
        if not self.settings.phone_line:
            raise CallError("No phone line configured: set ZADARMA_SIP_LOGIN/ZADARMA_SIP_PASSWORD or Twilio in .env")
        if not self.settings.voice_available:
            raise CallError("No voice backend (set GEMINI_API_KEY, start Ollama, or set OPENAI_API_KEY)")
        agent = get_agent_settings(self.db)
        if not agent["test_phone_number"] or phone != agent["test_phone_number"]:
            raise CallError("test calls can only go to the configured test_phone_number")
        call = self.db.create_call(phone=phone, lead_id=None, campaign_id=None, kind="test")
        return await self._dial(call, phone, agent)

    async def _dial(self, call: dict[str, Any], phone: str, agent: dict[str, Any]) -> dict[str, Any]:
        if self.settings.phone_line == "zadarma":
            try:
                placed = await self.zadarma.dial(call, phone)
            except Exception as exc:  # noqa: BLE001
                self.db.update_call(call["id"], status="failed", error=str(exc)[:500], ended_at=now_iso())
                if call.get("lead_id"):
                    self._finalize_lead(self.db.get_call(call["id"]))  # type: ignore[arg-type]
                raise CallError(str(exc)) from exc
            log.info("call %s placed to %s via Zadarma", call["id"], phone)
            return placed
        assert self.twilio is not None
        amd = "DetectMessageEnd" if agent["voicemail_action"] == "leave_message" else "Enable"
        try:
            sid = await self.twilio.create_call(to=phone, call_id=call["id"], machine_detection=amd)
        except (TwilioError, httpx.HTTPError) as exc:
            self.db.update_call(call["id"], status="failed", error=str(exc)[:500], ended_at=now_iso())
            if call.get("lead_id"):
                self._finalize_lead(self.db.get_call(call["id"]))  # type: ignore[arg-type]
            raise CallError(str(exc)) from exc
        log.info("call %s placed to %s (sid %s)", call["id"], phone, sid)
        return self.db.update_call(call["id"], twilio_sid=sid, status="initiated")  # type: ignore[return-value]

    async def place_whatsapp_call(self, phone: str, lead: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.whatsapp is None:
            raise CallError("WhatsApp calling is not configured (WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN)")
        if not self.settings.voice_available:
            raise CallError("No voice backend (set GEMINI_API_KEY, start Ollama, or set OPENAI_API_KEY)")
        if lead is not None:
            result = check_lead(self.db, lead, get_agent_settings(self.db))
            if not result.ok:
                raise CallError(f"cannot call now: {result.reason}")
        call = self.db.create_call(
            phone=phone, lead_id=lead["id"] if lead else None,
            campaign_id=lead.get("campaign_id") if lead else None, kind="whatsapp",
        )
        try:
            return await self.whatsapp.dial(call, phone)
        except Exception as exc:  # noqa: BLE001
            self.db.update_call(call["id"], status="failed", error=str(exc)[:500], ended_at=now_iso())
            raise CallError(str(exc)) from exc

    async def hangup(self, call_id: int) -> None:
        for line in (self.zadarma, self.whatsapp):
            if line is not None and await line.hangup(call_id):
                return
        session = self.sessions.get(call_id)
        if session is not None:
            session.request_hangup("operator")
            await session._do_hangup()
            return
        call = self.db.get_call(call_id)
        if call and call.get("twilio_sid") and self.twilio:
            await self.twilio.hangup(call["twilio_sid"])

    # ------------------------------------------------------------ carrier events
    def on_status(self, call_id: int, params: dict[str, str]) -> None:
        call = self.db.get_call(call_id)
        if call is None:
            return
        status = params.get("CallStatus", "")
        fields: dict[str, Any] = {}
        if status:
            if call["status"] in FINAL_STATUSES and status not in FINAL_STATUSES:
                return
            fields["status"] = status
        if status == "in-progress" and not call.get("answered_at"):
            fields["answered_at"] = now_iso()
        if params.get("AnsweredBy") and not call.get("answered_by"):
            fields["answered_by"] = params["AnsweredBy"]
        if status in FINAL_STATUSES:
            fields["ended_at"] = call.get("ended_at") or now_iso()
            if params.get("CallDuration"):
                fields["duration_s"] = int(params["CallDuration"])
            if status != "completed" and not call.get("outcome"):
                fields["outcome"] = {"busy": "busy", "no-answer": "no_answer"}.get(status, "failed")
            if params.get("ErrorMessage"):
                fields["error"] = params["ErrorMessage"][:500]
        updated = self.db.update_call(call_id, **fields)
        if status in FINAL_STATUSES and updated:
            self._finalize_lead(updated)

    async def on_amd(self, call_id: int, answered_by: str) -> None:
        self.db.update_call(call_id, answered_by=answered_by)
        if answered_by.startswith("machine") or answered_by == "fax":
            session = self.sessions.get(call_id)
            if session is not None:
                await session.handle_machine(answered_by)
            else:
                call = self.db.get_call(call_id)
                agent = get_agent_settings(self.db)
                if call and call.get("twilio_sid") and self.twilio and agent["voicemail_action"] == "hangup":
                    await self.twilio.hangup(call["twilio_sid"])

    # ------------------------------------------------------------- post-call
    async def on_session_finished(self, call_id: int) -> None:
        self.sessions.pop(call_id, None)
        call = self.db.get_call(call_id)
        if call is None:
            return
        fields: dict[str, Any] = {}
        if not call.get("ended_at"):
            fields["ended_at"] = now_iso()
        if call["kind"] == "practice":
            fields["status"] = "completed"
            started = parse_iso(call["created_at"])
            if started:
                fields["duration_s"] = int((utcnow() - started).total_seconds())
        elif not call.get("duration_s") and call.get("answered_at"):
            answered = parse_iso(call["answered_at"])
            if answered:
                fields["duration_s"] = int((utcnow() - answered).total_seconds())
        if fields:
            self.db.update_call(call_id, **fields)
        self.spawn(self.summarize_call(call_id))

    def _finalize_lead(self, call: dict[str, Any]) -> None:
        lead_id = call.get("lead_id")
        if not lead_id:
            return
        lead = self.db.get_lead(lead_id)
        if lead is None or lead["status"] != "calling":
            return
        campaign = self.db.get_campaign(lead["campaign_id"]) if lead.get("campaign_id") else None
        max_attempts = int(campaign["max_attempts"]) if campaign else 3
        retry_minutes = int(campaign["retry_minutes"]) if campaign else 1440
        answered_by = call.get("answered_by") or ""
        reached_human = call["status"] == "completed" and call.get("answered_at") and not (
            answered_by.startswith("machine") or answered_by == "fax" or call.get("outcome") == "voicemail"
        )
        if reached_human:
            self.db.update_lead(lead_id, status="contacted", status_reason=call.get("outcome") or "")
        elif lead["attempts"] >= max_attempts:
            self.db.update_lead(lead_id, status="exhausted", status_reason=f"no answer after {lead['attempts']} attempts")
        else:
            self.db.update_lead(
                lead_id,
                status="retry",
                status_reason=call.get("outcome") or call["status"],
                next_attempt_at=to_iso(utcnow() + timedelta(minutes=retry_minutes)),
            )

    async def summarize_call(self, call_id: int) -> None:
        transcript = self.db.get_transcript(call_id)
        spoken = [t for t in transcript if t["role"] in ("agent", "prospect")]
        if not any(t["role"] == "prospect" for t in spoken):
            call = self.db.get_call(call_id)
            if call and not call.get("outcome"):
                self.db.update_call(call_id, outcome="no_conversation")
            return
        text = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in transcript)
        try:
            if self.settings.openai_configured:
                body = {
                    "model": self.settings.summary_model,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": SUMMARY_PROMPT},
                        {"role": "user", "content": text[-60_000:]},
                    ],
                }
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                        json=body,
                    )
                    resp.raise_for_status()
                    data = json.loads(resp.json()["choices"][0]["message"]["content"])
            elif self.settings.uses_gemini:
                data = await GeminiClient(self.settings.gemini_api_key, self.settings.gemini_model).summarize_json(
                    SUMMARY_PROMPT, text[-60_000:]
                )
            elif self.settings.uses_openai_chat:
                from .openai_chat_llm import OpenAIChatClient

                data = await OpenAIChatClient(self.settings.openai_api_key, self.settings.summary_model).summarize_json(
                    SUMMARY_PROMPT, text[-60_000:]
                )
            elif self.settings.uses_ollama:
                data = await OllamaClient(self.settings.ollama_base_url, self.settings.ollama_model).summarize_json(
                    SUMMARY_PROMPT, text[-60_000:]
                )
            else:
                return
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("call %s: summary failed: %s", call_id, exc)
            return
        call = self.db.get_call(call_id) or {}
        summary = str(data.get("summary", ""))
        if data.get("coaching"):
            summary += f"\n\nCoaching: {data['coaching']}"
        fields: dict[str, Any] = {
            "summary": summary,
            "lead_score": str(data.get("lead_score", "")),
            "next_step": call.get("next_step") or str(data.get("next_step", "")),
        }
        if not call.get("outcome"):
            fields["outcome"] = str(data.get("outcome", ""))
        self.db.update_call(call_id, **fields)
        if call.get("lead_id"):
            lead = self.db.get_lead(call["lead_id"])
            if lead and not lead.get("score") and data.get("lead_score") in ("hot", "warm", "cold"):
                self.db.update_lead(lead["id"], score=data["lead_score"])
            if lead and lead["status"] == "contacted" and data.get("outcome") == "interested":
                self.db.update_lead(lead["id"], status="interested")
