"""Practice/call voice using Whisper + Kokoro/Piper + Ollama or Gemini (no OpenAI Realtime)."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import re
from typing import Any, Awaitable, Callable, Protocol

from .agent_settings import get_agent_settings
from .audio_util import EnergyVad, pcm16_chunk_b64, rms_s16, split_sentences
from .config import Settings
from .db import Database
from .gemini_llm import GeminiClient
from .ollama_llm import OllamaClient, speakable
from .openai_chat_llm import OpenAIChatClient
from .playbook import END_MARKER, build_compact_instructions, build_voice_instructions, get_playbook
from .realtime import Transport
from .stt import preload as preload_stt
from .stt import transcribe_pcm16
from .tools import ToolRunner, open_slots, spoken_time
from .tts import KokoroVoice, speak_pcm24k
from .vad import VAD_RATE, SileroEndpointer, VadEvent, silero_available


class _ChatLLM(Protocol):
    async def warmup(self) -> None: ...
    async def chat(
        self, messages: list[dict[str, Any]], *, tools: bool = True
    ) -> tuple[str, list[dict[str, Any]]]: ...
    def chat_stream(self, messages: list[dict[str, Any]]) -> Any: ...

log = logging.getLogger("agentvoice.local")

SAMPLE_RATE = 24000
PARTIAL_EVERY_BYTES = VAD_RATE * 2 * 7 // 10  # a fresh live transcript every ~0.7 s of speech
BARGE_IN_MS = 180  # stop the agent as soon as this much real speech overlaps it
HOLD_UNFINISHED_S = 1.2  # extra wait when they stop on "at", "and", "my"... they're still going
RETRACT_S = 2.5  # a reply interrupted this early was premature: forget it and keep listening
_UNFINISHED_WORDS = frozenset(
    "a an the and or but so to at of for with in on my our your is are was were be because like um uh "
    "if that than then from by as about i we you it's its i'm we're you're me just".split()
)


_BACKCHANNEL = frozenset("mm hm mhm mmhm uh huh um yeah yep yes ok okay right sure aha ah oh".split())


def looks_unfinished(text: str) -> bool:
    t = (text or "").strip()
    if t.endswith(("...", "…", ",", "-", "—")):
        return True
    words = re.findall(r"[a-z']+", t.lower())
    return bool(words) and words[-1] in _UNFINISHED_WORDS
ECHO_TAIL_S = 0.35  # the agent's voice can still come back through the mic just after playback

_DNC = re.compile(r"\b(don'?t|do not|never|stop) (ever )?call|remove me|take me off|put me on .*do.not.call|do.not.call list")
_BYE = re.compile(r"\b(good ?bye|bye|gotta go|got to go|have to go|hang up|talk later)\b")
_DECLINE = re.compile(
    r"\b(not interested|no,? thanks?|no,? thank you|not right now|not now|don'?t need|we'?re good|we are good|no need|pass)\b"
)
_SENTENCE_END = re.compile(r"[.!?](?=\s)")
_CLAUSE_END = re.compile(r"[,;:—](?=\s)")
_END_MARK = re.compile(r"\[?\s*END_CALL\s*\]?", re.I)
FIRST_CLAUSE_CHARS = 12  # speak the first clause early so the voice starts sooner
LONG_CLAUSE_CHARS = 45  # later sentences split at commas past this, keeping TTS chunks short


def _next_cut(text: str, first: bool) -> int | None:
    """End index of the next speakable unit in streamed text, or None to wait for more."""
    sentence = _SENTENCE_END.search(text)
    limit = sentence.end() if sentence else len(text)
    min_clause = FIRST_CLAUSE_CHARS if first else LONG_CLAUSE_CHARS
    for m in _CLAUSE_END.finditer(text, 0, limit):
        if m.end() >= min_clause:
            return m.end()
    return sentence.end() if sentence else None


def classify_intent(text: str) -> str | None:
    t = (text or "").lower()
    if _DNC.search(t):
        return "dnc"
    if _BYE.search(t):
        return "bye"
    if _DECLINE.search(t):
        return "decline"
    return None


class _Speculation:
    """A reply generated ahead of the confirmed end of turn."""

    def __init__(self) -> None:
        self.user_text = ""
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.error: Exception | None = None

    async def chunks(self) -> Any:
        while (chunk := await self.queue.get()) is not None:
            yield chunk
        if self.error is not None:
            raise self.error

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()


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
        llm: _ChatLLM | None = None,
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
        self.llm = llm or make_llm(settings)
        self._transcribe = transcribe or transcribe_pcm16
        self.kokoro = (
            KokoroVoice(settings.kokoro_model, settings.kokoro_voices, settings.kokoro_voice, settings.kokoro_speed)
            if settings.tts_engine == "kokoro" and speak is None
            else None
        )
        self._speak = speak or functools.partial(speak_pcm24k, kokoro=self.kokoro)
        # Gemini (and small Ollama) stay tool-free by default; Ollama tools only when explicitly on.
        self.use_tools = bool(settings.ollama_tools and settings.uses_ollama)
        # Small local models need per-turn steering; capable cloud models run on the playbook alone.
        self.scripted = settings.uses_ollama
        self._model_hangup = False
        self.declines = 0
        offer_times: list[str] = []
        if not self.use_tools:
            try:
                slots = open_slots(
                    db, self.agent_settings, self.tools.prospect_tz,
                    meeting_minutes=int(self.playbook.get("meeting_length_minutes", 20)), limit=2,
                )
                offer_times = [spoken_time(s) for s in slots]
            except Exception:  # noqa: BLE001
                log.exception("call %s: could not compute open slots", self.call_id)
        if self.scripted:
            instructions = build_compact_instructions(
                self.playbook, self.agent_settings, lead, campaign, direction=direction,
                tools=self.use_tools, offer_times=offer_times,
            )
        else:
            instructions = build_voice_instructions(
                self.playbook, self.agent_settings, lead, campaign, direction=direction,
                offer_times=offer_times,
            )
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": instructions}]
        self.endpointer = SileroEndpointer(in_rate=SAMPLE_RATE) if silero_available() else None
        self.vad = EnergyVad(sample_rate=SAMPLE_RATE)
        # Transcription started at a short pause, reused if the turn really ended there.
        self._early_stt: asyncio.Task[str] | None = None
        self._spec: _Speculation | None = None
        self._partial_task: asyncio.Task[Any] | None = None
        self._partial_at = 0
        self._tasks: list[asyncio.Task[Any]] = []
        self._turn_task: asyncio.Task[Any] | None = None
        self._finished = False
        self._hangup_reason: str | None = None
        self.caller_spoke = False
        self.agent_started = False
        self.voicemail_mode = False
        self._speaking = False
        # The phone plays audio long after we send it; this is when playback should finish.
        self._audible_until = 0.0
        self._turn_spoken: list[str] = []
        self._turn_text: list[str] = []  # everything queued for speech this turn (echo reference)
        self._barge_check: asyncio.Task[None] | None = None
        self._echo_rms = 0.0  # how loud the agent's own voice comes back through the mic
        # What the prospect has said this turn that hasn't been answered yet.
        self._user_buffer: list[str] = []
        self._turn_phase = ""  # "stt" | "hold" | "reply"
        self._reply_audio_at: float | None = None
        self._interrupted_playback = False

    def agent_audible(self) -> bool:
        return self._speaking or asyncio.get_running_loop().time() < self._audible_until + ECHO_TAIL_S

    async def _send_pcm(self, chunk: bytes) -> None:
        if not self._speaking:
            return
        now = asyncio.get_running_loop().time()
        if self._reply_audio_at is None:
            self._reply_audio_at = now
        self._audible_until = max(now, self._audible_until) + len(chunk) / (SAMPLE_RATE * 2)
        await self.transport.send_audio(base64.b64encode(chunk).decode())

    async def _barge_in(self) -> None:
        """Cut speaker output now. Do not start a new reply — wait until they finish talking."""
        if not self._speaking and self._audible_until == 0.0:
            return
        self._interrupted_playback = True
        self._speaking = False
        self._audible_until = 0.0
        await self.transport.clear()
        if self._turn_task and not self._turn_task.done() and self._turn_task is not asyncio.current_task():
            self._turn_task.cancel()
        log.info("call %s: agent stopped (interrupted)", self.call_id)

    async def interrupt(self) -> None:
        """Phone already muted itself; stop generating audio on this side too."""
        if self._finished:
            return
        log.info("call %s: barge-in (client)", self.call_id)
        await self._barge_in()

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self.llm.warmup(), name=f"llm-warm-{self.call_id}"))
        if self._transcribe is transcribe_pcm16:
            self._tasks.append(asyncio.create_task(preload_stt(self.settings.whisper_model), name=f"stt-warm-{self.call_id}"))
        if self.kokoro is not None:
            self._tasks.append(asyncio.create_task(self.kokoro.preload(), name=f"tts-warm-{self.call_id}"))
        self._tasks.append(asyncio.create_task(self._max_duration_guard(), name=f"local-guard-{self.call_id}"))
        if self.direction == "inbound":
            await self._respond("The prospect just called in. Greet them.")
        elif self.direction == "practice":
            # Practice: agent opens first (website discovery), don't wait for the user.
            self._tasks.append(asyncio.create_task(self._practice_kickoff(), name=f"local-kick-{self.call_id}"))
        else:
            self._tasks.append(asyncio.create_task(self._kickoff_if_silent(), name=f"local-kick-{self.call_id}"))
        log.info(
            "call %s: local voice session started (%s / %s)",
            self.call_id, self.direction, self.settings.llm_label,
        )

    async def _practice_kickoff(self) -> None:
        # Short beat so the WebSocket/mic is ready, then the agent starts talking.
        await asyncio.sleep(0.4)
        if self.caller_spoke or self.agent_started or self._finished:
            return
        if not self.scripted:
            await self._respond()
            return
        business = (self.lead or {}).get("business_name") or "your business"
        await self._respond(
            "The prospect just answered. YOU start the conversation: introduce yourself with name "
            f"and company, give the AI disclosure, ask if they have a quick minute about {business}, "
            "then ask whether they have a website and how it's working for them. Keep it to 2 short "
            "spoken sentences and end on that website question."
        )

    async def stop(self, reason: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        log.info("call %s: local session ending (%s)", self.call_id, reason or "stream closed")
        current = asyncio.current_task()
        if self._turn_task and self._turn_task is not current:
            self._turn_task.cancel()
        for task in (self._early_stt, self._partial_task, self._spec, self._barge_check):
            if task is not None:
                task.cancel()
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
        if self.endpointer is not None:
            for event in self.endpointer.push(pcm):
                await self._on_vad_event(event)
            await self._maybe_barge_in()
            self._maybe_partial()
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

    async def _maybe_barge_in(self) -> None:
        """Stop the agent the moment the prospect starts talking over it. Do not wait for STT."""
        ep = self.endpointer
        if ep is None or not self.agent_audible() or not ep.in_speech:
            return
        if ep.voiced_ms < BARGE_IN_MS:
            return
        log.info("call %s: barge-in (vad %s ms)", self.call_id, ep.voiced_ms)
        await self._barge_in()

    async def _on_vad_event(self, event: VadEvent) -> None:
        if event.kind == "start":
            self._partial_at = 0
            if self._turn_phase in ("hold", "reply") and not self.agent_audible():
                # They kept talking before we answered: drop the pending reply, their words join this turn.
                if self._turn_task and not self._turn_task.done():
                    self._turn_task.cancel()
        elif event.kind == "pause":
            if self.agent_audible():
                return
            self._early_stt = asyncio.create_task(self._transcribe(event.audio, VAD_RATE, self.settings.whisper_model))
            if not self.use_tools:
                self._spec = self._speculate(self._early_stt)
        elif event.kind == "resume":
            # Mid-sentence pause: the early result is now just a live partial.
            early, self._early_stt = self._early_stt, None
            if early is not None:
                early.add_done_callback(self._send_partial_from)
            if self._spec is not None:
                self._spec.cancel()
                self._spec = None
        elif event.kind == "end":
            if self.agent_audible():
                await self._barge_in()
            early, self._early_stt = self._early_stt, None
            spec, self._spec = self._spec, None
            if not event.audio:
                for task in (early, spec):
                    if task is not None:
                        task.cancel()
                if self._user_buffer and (self._turn_task is None or self._turn_task.done()):
                    self._turn_task = asyncio.create_task(self._commit_turn())
                return
            self.caller_spoke = True
            if self._turn_task and not self._turn_task.done():
                self._turn_task.cancel()
            self._turn_task = asyncio.create_task(self._handle_utterance(event.audio, VAD_RATE, early, spec))

    def _maybe_partial(self) -> None:
        """Live transcript while the prospect is still talking (only when the recognizer is idle)."""
        ep = self.endpointer
        # Only while clearly mid-word, so a partial rarely holds up the end-of-turn transcription.
        if ep is None or not ep.in_speech or self._early_stt is not None or ep.last_prob < 0.7:
            return
        if self.agent_audible():
            return  # the recognizer is busy telling echo from interruptions
        if self._partial_task is not None and not self._partial_task.done():
            return
        audio = ep.speech_so_far()
        if len(audio) - self._partial_at < PARTIAL_EVERY_BYTES:
            return
        self._partial_at = len(audio)
        self._partial_task = asyncio.create_task(self._transcribe(audio, VAD_RATE, self.settings.whisper_model))
        self._partial_task.add_done_callback(self._send_partial_from)

    def _send_partial_from(self, task: asyncio.Task[str]) -> None:
        if task.cancelled() or task.exception() is not None or self._finished:
            return
        text = task.result()
        if text:
            asyncio.ensure_future(
                self.transport.send_event({"type": "partial_transcript", "role": "prospect", "text": text})
            )

    async def _handle_utterance(
        self,
        pcm: bytes,
        rate: int = SAMPLE_RATE,
        early: asyncio.Task[str] | None = None,
        spec: _Speculation | None = None,
    ) -> None:
        try:
            await self._handle_utterance_inner(pcm, rate, early, spec)
        except asyncio.CancelledError:
            for task in (early, spec):
                if task is not None:
                    task.cancel()
            raise

    async def _handle_utterance_inner(
        self, pcm: bytes, rate: int, early: asyncio.Task[str] | None, spec: _Speculation | None
    ) -> None:
        self._turn_phase = "stt"
        try:
            if early is not None:
                text = await early
            else:
                text = await self._transcribe(pcm, rate, self.settings.whisper_model)
        except Exception as exc:  # noqa: BLE001
            log.exception("stt failed")
            await self.transport.send_event({"type": "error", "message": f"speech recognition failed: {exc}"})
            return
        if text and not self._is_echo(text, strict=True):
            self.db.add_transcript(self.call_id, "prospect", text)
            await self.transport.send_event({"type": "transcript", "role": "prospect", "text": text})
            self._user_buffer.append(text)
        await self._commit_turn(spec)

    async def _commit_turn(self, spec: _Speculation | None = None) -> None:
        """Answer everything said this turn, unless they're clearly not done yet."""
        try:
            if not self._user_buffer or (self.endpointer is not None and self.endpointer.in_speech):
                # Nothing to answer, or they're already talking again: the next end of speech commits.
                if spec is not None:
                    spec.cancel()
                return
            if looks_unfinished(self._user_buffer[-1]):
                if spec is not None:
                    spec.cancel()
                    spec = None
                self._turn_phase = "hold"
                await asyncio.sleep(HOLD_UNFINISHED_S)
            user_text = " ".join(self._user_buffer)
            self._user_buffer.clear()
            self._turn_phase = "reply"
            await self._respond(user_text=user_text, prefetched=spec)
        finally:
            self._turn_phase = ""

    async def _respond(
        self, system_nudge: str = "", user_text: str = "", prefetched: _Speculation | None = None
    ) -> None:
        if self._finished:
            return
        if not self.use_tools:
            await self._respond_lite(system_nudge, user_text, prefetched)
            return
        if prefetched is not None:
            prefetched.cancel()
        if system_nudge:
            self.messages.append({"role": "system", "content": system_nudge})
        if user_text:
            self.messages.append({"role": "user", "content": user_text})
        self.agent_started = True
        try:
            for _ in range(4):
                if self._finished:
                    return
                # Skip tools on pure talk turns — tool schemas make CPU Ollama ~2x slower.
                use_tools = self._wants_tools(user_text or system_nudge)
                text, calls = await self.llm.chat(self.messages, tools=use_tools)
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
                        # llama3.2 often fires end_call on openers/greetings — refuse until a real exchange.
                        if call["name"] == "end_call" and (
                            not self._agent_has_spoken() or not self.caller_spoke
                        ):
                            self.messages.append(
                                {
                                    "role": "tool",
                                    "tool_name": "end_call",
                                    "content": json.dumps(
                                        {
                                            "ok": False,
                                            "error": "Do not hang up yet. Keep talking until the prospect clearly says goodbye.",
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
            log.exception("llm turn failed")
            await self.transport.send_event({"type": "error", "message": f"LLM error: {exc}"})

    def _plan_turn(self, user_text: str, system_nudge: str = "") -> tuple[str | None, str, str]:
        """(intent, nudge, end_reason) for this turn. Pure, so it can run speculatively."""
        end_reason = ""
        intent = classify_intent(user_text) if user_text and self._agent_has_spoken() else None
        if intent == "dnc":
            nudge, end_reason = "They asked not to be called again. Apologize in one sentence, confirm they are removed, and say goodbye.", "do not call"
        elif not self.scripted:
            # The model handles the conversation from its playbook; rules only enforce hangups.
            nudge = ""
            if intent == "bye":
                end_reason = "prospect said goodbye"
        elif intent == "bye":
            nudge, end_reason = "They are ending the call. Thank them and say goodbye in one short sentence.", "prospect said goodbye"
        elif intent == "decline":
            if self.declines + 1 >= 2:
                nudge, end_reason = "They declined again. Thank them for their time and say goodbye in one short sentence.", "not interested"
            else:
                nudge = "They declined. Acknowledge it, give one short reason it could help them, and ask if a quick callback another day would work."
        else:
            nudge = ""
        return intent, " ".join(x for x in (system_nudge, nudge) if x), end_reason

    def _history_with(self, user_text: str) -> list[dict[str, Any]]:
        messages = list(self.messages)
        if user_text:
            messages.append({"role": "user", "content": user_text})
        elif not any(m.get("role") == "user" for m in messages):
            # Small models need a user turn to answer; the line opening sounds like a pick-up.
            messages.append({"role": "user", "content": "Hello?"})
        return messages

    @staticmethod
    def _with_nudge(messages: list[dict[str, Any]], nudge: str) -> list[dict[str, Any]]:
        # The turn instruction rides on the latest user message for this turn only: as a separate
        # system message a 1B model reads it aloud, and editing the system prompt defeats
        # Ollama's prompt cache.
        if not nudge:
            return messages
        last = messages[-1]
        return messages[:-1] + [{**last, "content": f"{last['content']}\n\n(Instruction for your reply, do not read aloud: {nudge})"}]

    def _speculate(self, early: asyncio.Task[str]) -> "_Speculation":
        """Start the reply while the prospect is (probably) done; playback waits for end of turn."""
        spec = _Speculation()

        async def run() -> None:
            try:
                text = await early
                if not text or self._finished:
                    return
                text = " ".join([*self._user_buffer, text])
                spec.user_text = text
                _intent, nudge, _end = self._plan_turn(text)
                async for chunk in self.llm.chat_stream(self._with_nudge(self._history_with(text), nudge)):
                    spec.queue.put_nowait(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                spec.error = exc
            finally:
                spec.queue.put_nowait(None)

        spec.task = asyncio.create_task(run())
        return spec

    async def _respond_lite(
        self, system_nudge: str = "", user_text: str = "", prefetched: "_Speculation | None" = None
    ) -> None:
        """Tool-free turn: rules decide call control, the model only talks (streamed)."""
        intent, nudge, end_reason = self._plan_turn(user_text, system_nudge)
        self.messages = self._history_with(user_text)
        turn_messages = self._with_nudge(self.messages, nudge)
        self.agent_started = True
        if intent == "dnc":
            result = await self.tools.run("add_to_do_not_call", json.dumps({"reason": "asked on call"}))
            await self.transport.send_event({"type": "tool", "name": "add_to_do_not_call", "result": result})
        elif intent == "decline":
            self.declines += 1

        self._model_hangup = False
        self._turn_spoken = []
        self._turn_text = []
        self._reply_audio_at = None
        self._interrupted_playback = False
        self._echo_rms *= 0.85  # let a stale echo estimate fade so it can't lock them out
        source = None
        if prefetched is not None and prefetched.user_text == user_text and not system_nudge:
            source = prefetched.chunks()
        elif prefetched is not None:
            prefetched.cancel()
        try:
            text = await self._stream_and_say(turn_messages, source)
        except asyncio.CancelledError:
            played = (
                asyncio.get_running_loop().time() - self._reply_audio_at if self._reply_audio_at is not None else 0.0
            )
            if user_text and played < RETRACT_S and not self._interrupted_playback:
                # We jumped in while they were still going: forget this reply, and let what they
                # say next join the same turn so the meaning isn't split.
                if self.messages and self.messages[-1].get("role") == "user" and self.messages[-1].get("content") == user_text:
                    self.messages.pop()
                self._user_buffer.insert(0, user_text)
            # Interrupted later: remember what was already said so the model doesn't start over.
            elif self._turn_spoken:
                said = " ".join(self._turn_spoken) + " —"
                self.messages.append({"role": "assistant", "content": said})
                self.db.add_transcript(self.call_id, "agent", said)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("llm turn failed")
            await self.transport.send_event({"type": "error", "message": f"LLM error: {exc}"})
            return
        if not text:
            text = "Sorry, could you say that again?" if not end_reason else "Thanks for your time. Goodbye."
            await self._say(text)
        self.messages.append({"role": "assistant", "content": text})
        self.db.add_transcript(self.call_id, "agent", text)
        await self.transport.send_event({"type": "transcript", "role": "agent", "text": text})

        if not end_reason and self._model_hangup:
            end_reason = "agent wrapped up"
        if end_reason:
            if end_reason == "not interested":
                await self.tools.run("set_outcome", json.dumps({"outcome": "not_interested", "notes": "declined twice"}))
            self.request_hangup(end_reason)
            await self.transport.mark("hangup")

    async def _stream_and_say(self, messages: list[dict[str, Any]], source: Any = None) -> str:
        """Speak each sentence as soon as the model finishes it; returns the full spoken text."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        speaker = asyncio.create_task(self._speak_queue(queue))
        spoken: list[str] = []
        pending = ""

        def flush(piece: str) -> None:
            clean = speakable(piece)
            # Keep a trailing clause comma so the voice still pauses there.
            if clean and piece.rstrip()[-1:] in ",;:":
                clean += ","
            if clean:
                spoken.append(clean)
                self._turn_text.append(clean)
                queue.put_nowait(clean)

        try:
            async for chunk in source if source is not None else self.llm.chat_stream(messages):
                pending += chunk
                if _END_MARK.search(pending):
                    self._model_hangup = True
                    pending = _END_MARK.sub(" ", pending)
                while (cut := _next_cut(pending, first=not spoken)) is not None:
                    flush(pending[:cut])
                    pending = pending[cut:]
            if "[" in pending:
                pending, fragment = pending[: pending.index("[")], pending[pending.index("["):]
                if fragment.upper().startswith("[END"):
                    self._model_hangup = True
            tail = pending.strip()
            # A tail without punctuation was cut off by num_predict; drop it if we already said something.
            if tail and (tail[-1] in ".!?" or not spoken):
                flush(tail)
            queue.put_nowait(None)
            await speaker
        except BaseException:
            speaker.cancel()
            raise
        return " ".join(spoken)

    async def _speak_queue(self, queue: asyncio.Queue[str | None]) -> None:
        self._speaking = True
        try:
            while (sentence := await queue.get()) is not None:
                if self._finished or not self._speaking:
                    return
                pcm = await self._speak(sentence, self.settings.piper_bin, self.settings.piper_model)
                for chunk in pcm16_chunk_b64(pcm or b""):
                    if self._finished or not self._speaking:
                        return
                    await self._send_pcm(chunk)
                self._turn_spoken.append(sentence)
            await self.transport.mark(f"audio-{self.call_id}")
        finally:
            self._speaking = False

    def _is_echo(self, text: str, strict: bool = False) -> bool:
        """The mic picked up the agent's own words (this turn or the last reply) from the speaker.

        strict: for a finished turn after playback, where short answers like "yeah" are real replies.
        """
        last = next((m.get("content") or "" for m in reversed(self.messages) if m.get("role") == "assistant"), "")
        heard = re.findall(r"[a-z']+", text.lower())
        said = set(re.findall(r"[a-z']+", f"{last} {' '.join(self._turn_text)}".lower()))
        if not heard:
            return not strict
        novel = [w for w in heard if w not in said and w not in _BACKCHANNEL]
        if strict:
            return len(heard) >= 3 and len(novel) <= 0.2 * len(heard)
        # Over the agent, it counts only if they said at least two words the agent didn't, and those
        # aren't drowned in echo (Whisper mixes the speaker's words into what they say).
        return len(novel) < 2 or len(novel) < 0.4 * len(heard)

    def _agent_has_spoken(self) -> bool:
        return any(m.get("role") == "assistant" and (m.get("content") or "").strip() for m in self.messages)

    def _wants_tools(self, latest: str) -> bool:
        text = (latest or "").lower()
        keys = (
            "book", "meeting", "schedule", "available", "availability", "callback",
            "call back", "email", "don't call", "do not call", "stop calling",
            "goodbye", "hang up", "not interested", "transfer", "human", "person",
        )
        if any(k in text for k in keys):
            return True
        # After a few exchanges, allow tools so booking can happen without magic words.
        user_turns = sum(1 for m in self.messages if m.get("role") == "user")
        return user_turns >= 3

    async def _say(self, text: str) -> None:
        self._speaking = True
        try:
            for sentence in split_sentences(text):
                if self._finished or not self._speaking:
                    return
                pcm = await self._speak(sentence, self.settings.piper_bin, self.settings.piper_model)
                if not pcm:
                    continue
                for chunk in pcm16_chunk_b64(pcm):
                    if self._finished or not self._speaking:
                        return
                    await self._send_pcm(chunk)
            await self.transport.mark(f"audio-{self.call_id}")
        finally:
            self._speaking = False


def make_llm(settings: Settings) -> _ChatLLM:
    if settings.uses_gemini:
        return GeminiClient(settings.gemini_api_key, settings.gemini_model)
    if settings.uses_openai_chat:
        return OpenAIChatClient(settings.openai_api_key, settings.summary_model)
    return OllamaClient(settings.ollama_base_url, settings.ollama_model)


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

    if settings.uses_local_voice:
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
