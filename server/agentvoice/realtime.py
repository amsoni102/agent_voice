"""Bridge between a caller audio transport (Twilio media stream or browser) and OpenAI Realtime."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .agent_settings import get_agent_settings
from .config import Settings
from .db import Database
from .playbook import build_instructions, get_playbook
from .tools import TOOL_DEFINITIONS, ToolRunner

log = logging.getLogger("agentvoice.realtime")

class Transport(ABC):
    """Where caller audio comes from and agent audio goes to."""

    #: OpenAI Realtime audio format object for both directions.
    audio_format: dict[str, Any]
    #: Bytes of encoded audio per millisecond (G.711 u-law 8 kHz = 8, PCM16 24 kHz = 48).
    bytes_per_ms: float

    @abstractmethod
    async def send_audio(self, b64_audio: str) -> None: ...

    @abstractmethod
    async def clear(self) -> None:
        """Drop any agent audio still buffered on the caller side (barge-in)."""

    @abstractmethod
    async def mark(self, name: str) -> None:
        """Ask the transport to echo `name` back once all audio sent so far has played."""

    @abstractmethod
    async def hangup(self) -> None: ...

    async def transfer(self, number: str) -> bool:
        return False

    async def send_event(self, event: dict[str, Any]) -> None:
        """Optional side-channel for UI events (live transcript etc.)."""


class VoiceSession:
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
    ):
        self.settings = settings
        self.db = db
        self.call = call
        self.call_id: int = call["id"]
        self.lead = lead
        self.campaign = campaign
        self.transport = transport
        self.direction = direction  # outbound | inbound | practice
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

        self.ws: ClientConnection | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._send_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._finished = False

        self.caller_spoke = False
        self.agent_started = False
        self.voicemail_mode = False

        # Playback tracking for barge-in truncation.
        self._item_id: str | None = None
        self._audio_started_at: float | None = None
        self._audio_ms_sent = 0.0
        self._interrupted_items: set[str] = set()

        self._response_active = False
        self._pending_tool_followup = False
        self._hangup_reason: str | None = None
        self._hangup_mark_sent = False
        self._mark_seq = 0

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        url = f"{self.settings.realtime_url}?model={self.settings.realtime_model}"
        self.ws = await connect(
            url,
            additional_headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            max_size=None,
            ping_interval=20,
        )
        await self._send(self._session_update())
        self._tasks.append(asyncio.create_task(self._read_openai(), name=f"rt-read-{self.call_id}"))
        self._tasks.append(asyncio.create_task(self._max_duration_guard(), name=f"rt-guard-{self.call_id}"))
        if self.direction == "inbound":
            await self._create_response()
        else:
            self._tasks.append(asyncio.create_task(self._kickoff_if_silent(), name=f"rt-kick-{self.call_id}"))
        log.info("call %s: realtime session started (%s)", self.call_id, self.direction)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def stop(self, reason: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        log.info("call %s: session ending (%s)", self.call_id, reason or "stream closed")
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._closed.set()
        if self.on_finished is not None:
            try:
                await self.on_finished(self.call_id)
            except Exception:  # noqa: BLE001
                log.exception("call %s: on_finished failed", self.call_id)

    # ------------------------------------------------------- caller -> model
    async def feed_audio(self, b64_audio: str) -> None:
        if self.ws is None or self._finished:
            return
        await self._send({"type": "input_audio_buffer.append", "audio": b64_audio})

    async def on_mark(self, name: str) -> None:
        if name == "hangup" and self._hangup_reason is not None:
            await self._do_hangup()

    async def handle_machine(self, answered_by: str) -> None:
        """Answering-machine detection result from the carrier."""
        if self.voicemail_mode or self._finished:
            return
        self.voicemail_mode = True
        self.db.update_call(self.call_id, outcome="voicemail")
        if self.agent_settings["voicemail_action"] != "leave_message" or not answered_by.startswith("machine_end"):
            self.request_hangup("voicemail")
            await self._do_hangup()
            return
        await self._cancel_and_clear()
        pb = self.playbook
        callback = pb.get("callback_number") or "this number"
        await self.inject_system(
            "You have reached the prospect's voicemail and the beep has sounded. Leave a friendly, natural "
            "voicemail under 20 seconds: your name, the company, the disclosure that you're an AI assistant, "
            f"one sentence on why you called tailored to their business, and that they can reach us at {callback}. "
            "Then call end_call with reason 'voicemail left'.",
            respond=True,
        )

    # ---------------------------------------------------------- tool host API
    def request_hangup(self, reason: str) -> None:
        if self._hangup_reason is None:
            self._hangup_reason = reason or "ended"
            self._tasks.append(asyncio.create_task(self._hangup_fallback()))

    async def request_transfer(self, number: str, reason: str) -> None:
        async def later() -> None:
            await asyncio.sleep(2.5)
            ok = await self.transport.transfer(number)
            if not ok:
                self.request_hangup("transfer failed")

        self._tasks.append(asyncio.create_task(later()))

    async def inject_system(self, text: str, respond: bool) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]},
            }
        )
        if respond:
            await self._create_response()

    # --------------------------------------------------------------- internals
    def _session_update(self) -> dict[str, Any]:
        s = self.agent_settings
        instructions = build_instructions(
            self.playbook, s, self.lead, self.campaign, direction=self.direction
        )
        eagerness = s.get("turn_eagerness", "auto")
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.settings.realtime_model,
                "output_modalities": ["audio"],
                "instructions": instructions,
                "audio": {
                    "input": {
                        "format": self.transport.audio_format,
                        "noise_reduction": {"type": "near_field"},
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": eagerness,
                            "create_response": True,
                            "interrupt_response": True,
                        },
                    },
                    "output": {
                        "format": self.transport.audio_format,
                        "voice": s["voice"],
                        "speed": s["speech_speed"],
                    },
                },
                "tools": TOOL_DEFINITIONS,
                "tool_choice": "auto",
            },
        }

    async def _send(self, event: dict[str, Any]) -> None:
        if self.ws is None:
            return
        async with self._send_lock:
            try:
                await self.ws.send(json.dumps(event))
            except ConnectionClosed:
                pass

    async def _create_response(self) -> None:
        if self._finished:
            return
        self.agent_started = True
        await self._send({"type": "response.create"})

    async def _kickoff_if_silent(self) -> None:
        await asyncio.sleep(float(self.agent_settings["silence_kickoff_seconds"]))
        if not self.caller_spoke and not self.agent_started and not self.voicemail_mode:
            await self._create_response()

    async def _max_duration_guard(self) -> None:
        limit = int(self.agent_settings["max_call_minutes"]) * 60
        await asyncio.sleep(max(30, limit - 45))
        await self.inject_system(
            "We are almost out of time for this call. Wrap up naturally in one or two sentences, "
            "confirm any next step, say goodbye and call end_call.",
            respond=not self._response_active,
        )
        await asyncio.sleep(60)
        self.request_hangup("max duration")
        await self._do_hangup()

    async def _hangup_fallback(self) -> None:
        remaining_ms = 0.0
        if self._audio_started_at is not None:
            played = (time.monotonic() - self._audio_started_at) * 1000
            remaining_ms = max(0.0, self._audio_ms_sent - played)
        await asyncio.sleep(min(20.0, 6.0 + remaining_ms / 1000))
        await self._do_hangup()

    async def _do_hangup(self) -> None:
        if self._finished:
            return
        try:
            await self.transport.hangup()
        finally:
            await self.stop(self._hangup_reason or "hangup")

    async def _cancel_and_clear(self) -> None:
        if self._response_active:
            await self._send({"type": "response.cancel"})
        await self._truncate_playback()

    async def _truncate_playback(self) -> None:
        if self._item_id and self._audio_started_at is not None:
            played_ms = int(min((time.monotonic() - self._audio_started_at) * 1000, self._audio_ms_sent))
            if played_ms < self._audio_ms_sent - 50:
                self._interrupted_items.add(self._item_id)
                await self._send(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": self._item_id,
                        "content_index": 0,
                        "audio_end_ms": max(0, played_ms),
                    }
                )
                await self.transport.clear()
        self._item_id = None
        self._audio_started_at = None
        self._audio_ms_sent = 0.0

    async def _read_openai(self) -> None:
        assert self.ws is not None
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                try:
                    await self._handle_event(event)
                except Exception:  # noqa: BLE001
                    log.exception("call %s: error handling %s", self.call_id, event.get("type"))
        except ConnectionClosed as exc:
            log.warning("call %s: OpenAI connection closed: %s", self.call_id, exc)
        finally:
            if not self._finished:
                self.db.update_call(self.call_id, error="voice model connection closed")
                self.request_hangup("model disconnected")
                await self._do_hangup()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")

        if etype in ("response.output_audio.delta", "response.audio.delta"):
            item_id = event.get("item_id")
            if item_id != self._item_id:
                self._item_id = item_id
                self._audio_started_at = time.monotonic()
                self._audio_ms_sent = 0.0
            delta = event.get("delta", "")
            self._audio_ms_sent += (len(delta) * 3 / 4) / self.transport.bytes_per_ms
            await self.transport.send_audio(delta)

        elif etype in ("response.output_audio.done", "response.audio.done"):
            self._mark_seq += 1
            await self.transport.mark(f"audio-{self._mark_seq}")

        elif etype == "input_audio_buffer.speech_started":
            self.caller_spoke = True
            await self._truncate_playback()
            await self.transport.send_event({"type": "speech_started"})

        elif etype == "conversation.item.input_audio_transcription.completed":
            text = event.get("transcript", "")
            self.db.add_transcript(self.call_id, "prospect", text)
            await self.transport.send_event({"type": "transcript", "role": "prospect", "text": text})

        elif etype in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            text = event.get("transcript", "")
            if event.get("item_id") in self._interrupted_items:
                text = text.rstrip() + " [interrupted]"
            self.db.add_transcript(self.call_id, "agent", text)
            await self.transport.send_event({"type": "transcript", "role": "agent", "text": text})

        elif etype == "response.created":
            self._response_active = True
            self.agent_started = True

        elif etype == "response.function_call_arguments.done":
            name = event.get("name", "")
            result = await self.tools.run(name, event.get("arguments", "{}"))
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": event.get("call_id"),
                        "output": json.dumps(result),
                    },
                }
            )
            await self.transport.send_event({"type": "tool", "name": name, "result": result})
            if name != "end_call":
                self._pending_tool_followup = True

        elif etype == "response.done":
            self._response_active = False
            status = (event.get("response") or {}).get("status")
            if self._hangup_reason is not None:
                if not self._hangup_mark_sent:
                    self._hangup_mark_sent = True
                    await self.transport.mark("hangup")
                self._pending_tool_followup = False
            elif self._pending_tool_followup and status != "cancelled":
                self._pending_tool_followup = False
                await self._create_response()

        elif etype == "error":
            err = event.get("error") or {}
            code = err.get("code", "")
            if code in ("response_cancel_not_active", "conversation_already_has_active_response"):
                log.debug("call %s: benign realtime error %s", self.call_id, code)
            else:
                log.error("call %s: realtime error: %s", self.call_id, err)
                self.db.update_call(self.call_id, error=str(err.get("message", err))[:500])

        elif etype == "session.created" or etype == "session.updated":
            log.debug("call %s: %s", self.call_id, etype)

