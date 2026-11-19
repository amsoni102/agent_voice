"""Practice/call voice using local Ollama + Whisper + Piper (no OpenAI Realtime)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Awaitable, Callable

from .agent_settings import get_agent_settings
from .audio_util import EnergyVad, pcm16_chunk_b64, rms_s16, split_sentences
from .config import Settings
from .db import Database
from .ollama_llm import OllamaClient
from .playbook import build_instructions, get_playbook
from .realtime import Transport
from .stt import transcribe_pcm16
from .tools import ToolRunner
from .tts import speak_pcm24k

log = logging.getLogger("agentvoice.local")

SAMPLE_RATE = 24000


class LocalVoiceSession:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        call: dict[str, Any],
        lead: dict[str, Any] | None,
        campaign: dict[str, Any] | None,
        transport: Transport,
        direction: str,
        on_finished: Callable[[int], Awaitable[None]] | None = None,
        llm: OllamaClient | None = None,
        transcribe: Any = None,
        speak: Any = None,
    ):
        self.settings = settings
        self.db = db
        self.call = call
        self.call_id: int = call["id"]
        self.lead = lead
        self.campaign = campaign
        self.transport = transport
        self.direction = direction
        self.on_finished = on_finished
        self.agent_settings = get_agent_settings(db)
        self.playbook = get_playbook(db)
        self.tools = ToolRunner(
            db=db,
            host=self,
            call_id=self.call_id,
            lead=lead,
            agent_settings=self.agent_settings,
            playbook=self.playbook,
            dry_run=direction == "practice",
        )
        self.llm = llm or OllamaClient(settings.ollama_base_url, settings.ollama_model)
        self._transcribe = transcribe or transcribe_pcm16
        self._speak = speak or speak_pcm24k
        instructions = build_instructions(self.playbook, self.agent_settings, lead, campaign, direction=direction)
        extra = (
            "\n\nYou are on a live voice call. Always speak out loud in one or two short sentences "
            "before calling any tool. Do not use markdown, bullets, or URLs. "
            "Only call a tool when the prospect asked for times, a booking, a callback, DNC, or to hang up."
        )
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": instructions + extra}]
        self.vad = EnergyVad(sample_rate=SAMPLE_RATE)
        self._tasks: list[asyncio.Task[Any]] = []
        self._turn_task: asyncio.Task[Any] | None = None
        self._finished = False
        self._hangup_reason: str | None = None
        self.caller_spoke = False
        self.agent_started = False
        self.voicemail_mode = False
        self._speaking = False

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self.llm.warmup(), name=f"ollama-warm-{self.call_id}"))
        self._tasks.append(asyncio.create_task(self._max_duration_guard(), name=f"local-guard-{self.call_id}"))
        if self.direction == "inbound":
            await self._respond("The prospect just called in. Greet them.")
        else:
            self._tasks.append(asyncio.create_task(self._kickoff_if_silent(), name=f"local-kick-{self.call_id}"))
        log.info("call %s: local Ollama session started (%s / %s)", self.call_id, self.direction, self.settings.ollama_model)

    async def stop(self, reason: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        log.info("call %s: local session ending (%s)", self.call_id, reason or "stream closed")
        current = asyncio.current_task()
        if self._turn_task and self._turn_task is not current:
            self._turn_task.cancel()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        if self.on_finished is not None:
            try:
                await self.on_finished(self.call_id)
            except Exception:  # noqa: BLE001
                log.exception("call %s: on_finished failed", self.call_id)

    async def feed_audio(self, b64_audio: str) -> None:
        if self._finished:
            return
        try:
            pcm = base64.b64decode(b64_audio)
        except Exception:  # noqa: BLE001
            return
        if self._speaking and rms_s16(pcm) > 800:
            await self.transport.clear()
            if self._turn_task:
                self._turn_task.cancel()
            self._speaking = False
            self.vad.reset()
        utterance = self.vad.push(pcm)
        if not utterance:
            return
        self.caller_spoke = True
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        self._turn_task = asyncio.create_task(self._handle_utterance(utterance))

    async def on_mark(self, name: str) -> None:
        if name == "hangup" and self._hangup_reason is not None:
            await self._do_hangup()

    async def handle_machine(self, answered_by: str) -> None:
        if self.voicemail_mode or self._finished:
            return
        self.voicemail_mode = True
        self.db.update_call(self.call_id, outcome="voicemail")
        if self.agent_settings["voicemail_action"] != "leave_message" or not answered_by.startswith("machine_end"):
            self.request_hangup("voicemail")
            await self._do_hangup()
            return
        await self.transport.clear()
        await self._respond("You reached voicemail. Leave a short message, then end_call.")

    def request_hangup(self, reason: str) -> None:
        if self._hangup_reason is None:
            self._hangup_reason = reason or "ended"
            self._tasks.append(asyncio.create_task(self._hangup_fallback()))

    async def request_transfer(self, number: str, reason: str) -> None:
        async def later() -> None:
            await asyncio.sleep(2.5)
            if not await self.transport.transfer(number):
                self.request_hangup("transfer failed")

        self._tasks.append(asyncio.create_task(later()))

    async def inject_system(self, text: str, respond: bool) -> None:
        if respond:
            await self._respond(text)

    async def _do_hangup(self) -> None:
        if self._finished:
            return
        try:
            await self.transport.hangup()
        finally:
            await self.stop(self._hangup_reason or "hangup")

    async def _hangup_fallback(self) -> None:
        await asyncio.sleep(4)
        await self._do_hangup()

    async def _kickoff_if_silent(self) -> None:
        await asyncio.sleep(float(self.agent_settings.get("silence_kickoff_seconds") or 3))
        if not self.caller_spoke and not self.agent_started and not self._finished:
            await self._respond("The line is open. The prospect has not spoken yet. Give your opening.")

    async def _max_duration_guard(self) -> None:
        limit = int(self.agent_settings.get("max_call_minutes") or 12) * 60
        await asyncio.sleep(max(30, limit - 45))
        if not self._finished:
            await self._respond("Almost out of time. Wrap up in one sentence, then end_call.")
        await asyncio.sleep(45)
        self.request_hangup("max duration")
        await self._do_hangup()

    async def _handle_utterance(self, pcm: bytes) -> None:
        try:
            text = await self._transcribe(pcm, SAMPLE_RATE, self.settings.whisper_model)
        except Exception as exc:  # noqa: BLE001
            log.exception("stt failed")
            await self.transport.send_event({"type": "error", "message": f"speech recognition failed: {exc}"})
            return
        if not text:
            return
        self.db.add_transcript(self.call_id, "prospect", text)
        await self.transport.send_event({"type": "transcript", "role": "prospect", "text": text})
        await self._respond(user_text=text)

    async def _respond(self, system_nudge: str = "", user_text: str = "") -> None:
        if self._finished:
            return
        if system_nudge:
            self.messages.append({"role": "system", "content": system_nudge})
        if user_text:
            self.messages.append({"role": "user", "content": user_text})
        self.agent_started = True
        try:
            for _ in range(4):
                if self._finished:
                    return
                text, calls = await self.llm.chat(self.messages)
                if calls:
                    assistant_msg: dict[str, Any] = {"role": "assistant", "content": text}
                    ollama_calls = []
                    for call in calls:
                        ollama_calls.append(
                            {"function": {"name": call["name"], "arguments": call.get("arguments") or {}}}
                        )
                    assistant_msg["tool_calls"] = ollama_calls
                    self.messages.append(assistant_msg)
                    for call in calls:
                        # llama3.2 often fires end_call on a greeting — refuse until we've spoken.
                        if call["name"] == "end_call" and not self._agent_has_spoken():
                            self.messages.append(
                                {
                                    "role": "tool",
                                    "tool_name": "end_call",
                                    "content": json.dumps(
                                        {
                                            "ok": False,
                                            "error": "Speak your opening first. Only end_call after a real goodbye.",
                                        }
                                    ),
                                }
                            )
                            continue
                        result = await self.tools.run(call["name"], json.dumps(call.get("arguments") or {}))
                        await self.transport.send_event({"type": "tool", "name": call["name"], "result": result})
                        self.messages.append(
                            {
                                "role": "tool",
                                "tool_name": call["name"],
                                "content": json.dumps(result),
                            }
                        )
                        if call["name"] == "end_call":
                            if text:
                                await self._say(text)
                            self.request_hangup(str((call.get("arguments") or {}).get("reason") or "ended"))
                            await self.transport.mark("hangup")
                            return
                    continue
                if text:
                    self.messages.append({"role": "assistant", "content": text})
                    self.db.add_transcript(self.call_id, "agent", text)
                    await self.transport.send_event({"type": "transcript", "role": "agent", "text": text})
                    await self._say(text)
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("ollama turn failed")
            await self.transport.send_event({"type": "error", "message": f"Ollama error: {exc}"})

    def _agent_has_spoken(self) -> bool:
        return any(m.get("role") == "assistant" and (m.get("content") or "").strip() for m in self.messages)

    async def _say(self, text: str) -> None:
        self._speaking = True
        try:
            for sentence in split_sentences(text):
                if self._finished:
                    return
                pcm = await self._speak(sentence, self.settings.piper_bin, self.settings.piper_model)
                if not pcm:
                    continue
                for chunk in pcm16_chunk_b64(pcm):
                    if self._finished:
                        return
                    await self.transport.send_audio(base64.b64encode(chunk).decode())
            await self.transport.mark(f"audio-{self.call_id}")
        finally:
            self._speaking = False


def create_voice_session(
    *,
    settings: Settings,
    db: Database,
    call: dict[str, Any],
    lead: dict[str, Any] | None,
    campaign: dict[str, Any] | None,
    transport: Transport,
    direction: str,
    on_finished: Callable[[int], Awaitable[None]] | None = None,
) -> LocalVoiceSession | Any:
    from .realtime import VoiceSession

    if settings.uses_ollama:
        return LocalVoiceSession(
            settings=settings,
            db=db,
            call=call,
            lead=lead,
            campaign=campaign,
            transport=transport,
            direction=direction,
            on_finished=on_finished,
        )
    return VoiceSession(
        settings=settings,
        db=db,
        call=call,
        lead=lead,
        campaign=campaign,
        transport=transport,
        direction=direction,
        on_finished=on_finished,
    )
