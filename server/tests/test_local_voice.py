import asyncio
import base64

import random
import struct

import pytest

from agentvoice.audio_util import EnergyVad, resample_s16, split_sentences
from agentvoice.local_voice import LocalVoiceSession, classify_intent, looks_unfinished
from agentvoice.vad import SileroEndpointer, VadEvent
from agentvoice.ollama_llm import _window, chat_tools, clean_tool_args, speakable
from agentvoice.realtime import Transport


def test_auto_backend_uses_ollama_without_openai_key(make_settings):
    s = make_settings(openai_api_key="", gemini_api_key="", voice_backend="auto")
    assert s.uses_ollama and s.voice_available and not s.uses_gemini
    s2 = make_settings(openai_api_key="sk-test", gemini_api_key="", voice_backend="auto")
    assert s2.uses_openai_chat and s2.uses_local_voice and not s2.uses_openai_realtime
    s3 = make_settings(openai_api_key="sk-test", voice_backend="ollama")
    assert s3.uses_ollama
    s4 = make_settings(openai_api_key="", gemini_api_key="AIza-test", voice_backend="auto")
    assert s4.uses_gemini and s4.uses_local_voice and not s4.uses_ollama
    s5 = make_settings(openai_api_key="", gemini_api_key="AIza-test", voice_backend="gemini")
    assert s5.uses_gemini and s5.llm_label.startswith("gemini:")
    s6 = make_settings(openai_api_key="sk-test", gemini_api_key="", voice_backend="openai")
    assert s6.uses_openai_realtime and not s6.uses_local_voice


def test_chat_tools_match_playbook_tools():
    names = {t["function"]["name"] for t in chat_tools()}
    assert {"book_meeting", "end_call", "add_to_do_not_call", "record_lead_info"} <= names


def test_classify_intent():
    assert classify_intent("No, thank you.") == "decline"
    assert classify_intent("We're not interested") == "decline"
    assert classify_intent("Please don't call me again") == "dnc"
    assert classify_intent("Take me off your list") == "dnc"
    assert classify_intent("Okay, bye") == "bye"
    assert classify_intent("Okay, and why are you calling me?") is None
    assert classify_intent("No, we don't have a website") is None


def test_speakable_strips_json_and_tool_names():
    assert speakable('{"next_available_time": "Thu 3:30 PM"}') == ""
    assert speakable('Sure. {"name": "end_call"} Bye!') == "Sure. Bye!"
    assert speakable("**Great** question") == "Great question"


def test_clean_tool_args_drops_schema_echo():
    assert clean_tool_args({"type": "object", "function": {}, "parameters": {}}) == {}
    assert clean_tool_args({"date": "2026-11-20", "type": "x"}) == {"date": "2026-11-20"}
    assert clean_tool_args("nope") == {}


def test_window_keeps_mid_call_system_nudges():
    msgs = [{"role": "system", "content": "main"}] + [{"role": "user", "content": str(i)} for i in range(12)]
    msgs.append({"role": "system", "content": "nudge"})
    out = _window(msgs)
    assert out[0]["content"] == "main" and out[-1]["content"] == "nudge" and len(out) == 11


def test_sentence_split():
    assert split_sentences("Hi there. Got a minute?") == ["Hi there.", "Got a minute?"]


def test_resample_roundtrip_length():
    pcm = b"\x00\x01" * 2400  # 100 ms at 24 kHz
    down = resample_s16(pcm, 24000, 16000)
    assert abs(len(down) / 2 - 1600) <= 2


def test_energy_vad_captures_utterance():
    vad = EnergyVad(sample_rate=24000)
    loud = b"\xff\x7f" * 480
    quiet = b"\x00\x00" * 480
    assert vad.push(loud * 8) is None
    got = vad.push(quiet * 50)
    assert got is not None and len(got) > 1000


class FakeTransport(Transport):
    audio_format = {"type": "audio/pcm", "rate": 24000}
    bytes_per_ms = 48.0

    def __init__(self):
        self.audio = []
        self.events = []
        self.cleared = 0
        self.hung_up = False

    async def send_audio(self, b64_audio):
        self.audio.append(b64_audio)

    async def clear(self):
        self.cleared += 1

    async def mark(self, name):
        self.events.append(("mark", name))

    async def hangup(self):
        self.hung_up = True

    async def send_event(self, event):
        self.events.append(event)


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def warmup(self):
        return None

    async def chat(self, messages, *, tools=True):
        self.calls += 1
        user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        if self.calls == 1:
            return "Hey, it's Alex with Stellar Technologies.", []
        if "not interested" in user.lower():
            return "No problem.", [{"name": "end_call", "arguments": {"reason": "not interested"}}]
        return "Got it.", []

    async def chat_stream(self, messages):
        text, _ = await self.chat(messages, tools=False)
        for word in text.split(" "):
            yield word + " "


def make_session(db, settings, transport, llm, transcript_text, spoken, on_finished=None):
    call = db.create_call(phone="", lead_id=None, campaign_id=None, kind="practice")

    async def transcribe(pcm, rate, model):
        return transcript_text[0]

    async def speak(text, _bin, _model):
        spoken.append(text)
        return b"\x00\x01" * 480

    return LocalVoiceSession(
        settings=settings, db=db, call=call, lead=None, campaign=None, transport=transport,
        direction="practice", on_finished=on_finished, llm=llm, transcribe=transcribe, speak=speak,
    )


async def test_local_session_declining_twice_ends_call(db, make_settings):
    settings = make_settings(openai_api_key="", voice_backend="ollama")
    transport, spoken, said = FakeTransport(), [], ["Hello?"]
    session = make_session(db, settings, transport, FakeLLM(), said, spoken)
    await session.start()
    await session._respond(user_text="Hello?")
    said[0] = "No, thank you."
    await session._respond(user_text="No, thank you.")
    assert session._hangup_reason is None
    await session._respond(user_text="No thanks, not interested.")
    assert session._hangup_reason == "not interested"
    assert ("mark", "hangup") in transport.events
    assert not any("{" in s for s in spoken)
    await session.stop("test")


class ScriptLLM(FakeLLM):
    def __init__(self, replies):
        super().__init__()
        self.replies = replies
        self.seen = []

    async def chat_stream(self, messages):
        self.seen.append(messages[-1]["content"])
        for piece in self.replies.pop(0):
            yield piece


async def test_cloud_model_gets_no_canned_nudge_and_can_end_the_call(db, make_settings):
    settings = make_settings(voice_backend="gemini", gemini_api_key="g-test")
    transport, spoken = FakeTransport(), []
    llm = ScriptLLM([
        ["Hey, it's Alex with Stellar Technologies, ", "got a quick minute? "],
        ["Totally get it, thanks for your time. Have a good one! [END", "_CALL]"],
    ])
    session = make_session(db, settings, transport, llm, ["x"], spoken)
    assert "Delaware" in session.messages[0]["content"]
    await session._respond(user_text="Hello?")
    await session._respond(user_text="No thanks, not interested.")
    assert llm.seen[-1] == "No thanks, not interested."
    assert spoken[:2] == ["Hey, it's Alex with Stellar Technologies,", "got a quick minute?"]
    assert not any("END" in s for s in spoken)
    assert session._hangup_reason == "agent wrapped up"
    await session.stop("test")


async def test_echo_while_agent_plays_is_ignored_and_interruption_is_remembered(db, make_settings, monkeypatch):
    settings = make_settings(voice_backend="gemini", gemini_api_key="g-test")
    transport, spoken = FakeTransport(), []
    llm = ScriptLLM([["Hi, it's Alex with Stellar Technologies. We build websites and apps. "]])
    session = make_session(db, settings, transport, llm, ["x"], spoken)
    await session._respond()
    assert session.agent_audible()  # the phone is still playing what was sent
    assert session._is_echo("it's Alex with Stellar Technologies")
    assert not session._is_echo("yeah who is this")

    async def slow_stream(messages):
        yield "Totally, so we build websites. "
        await asyncio.sleep(10)
        yield "never said"

    llm.chat_stream = slow_stream

    # Cut off right away: the reply was premature, so it's dropped and their words wait for more.
    turn = asyncio.create_task(session._respond(user_text="Who is this?"))
    await wait_for(lambda: len(spoken) >= 3)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert session._user_buffer == ["Who is this?"]
    assert session.messages[-1]["content"] != "Who is this?"
    assert "so we build websites" not in session.messages[-1]["content"]

    # Cut off after playing a while: keep what was said so the model doesn't start over.
    session._user_buffer.clear()
    monkeypatch.setattr("agentvoice.local_voice.RETRACT_S", 0.0)
    turn = asyncio.create_task(session._respond(user_text="Who is this?"))
    await wait_for(lambda: len(spoken) >= 5)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert session.messages[-1]["role"] == "assistant"
    assert "so we build websites" in session.messages[-1]["content"]
    await session.stop("test")


async def test_unfinished_sentence_waits_and_fragments_join_one_turn(db, make_settings, monkeypatch):
    monkeypatch.setattr("agentvoice.local_voice.HOLD_UNFINISHED_S", 0.2)
    settings = make_settings(voice_backend="gemini", gemini_api_key="g-test")
    transport, spoken, heard = FakeTransport(), [], ["Yeah, can you send me an email at?"]
    llm = ScriptLLM([["Sure, what's the address? "]])
    session = make_session(db, settings, transport, llm, heard, spoken)
    session.messages.append({"role": "assistant", "content": "Want me to send you some info?"})
    assert looks_unfinished("Yeah, can you send me an email at?") and not looks_unfinished("Sure, sounds good.")

    first = session._turn_task = asyncio.create_task(session._handle_utterance(b"\x00" * 32000, 16000))
    await asyncio.sleep(0.05)
    assert session._turn_phase == "hold" and llm.seen == []
    # They keep talking during the hold: the pending turn yields, the next piece completes it.
    await session._on_vad_event(VadEvent("start"))
    await asyncio.sleep(0)
    assert first.cancelled() or first.done()
    heard[0] = "john at gmail dot com."
    await session._handle_utterance(b"\x00" * 32000, 16000)
    assert llm.seen == ["Yeah, can you send me an email at? john at gmail dot com."]
    await session.stop("test")


async def test_interrupt_stops_agent_immediately_without_stt(db, make_settings):
    settings = make_settings(voice_backend="gemini", gemini_api_key="g-test")
    transport, spoken, heard = FakeTransport(), [], ["x"]
    llm = ScriptLLM([["We build websites and mobile apps for local businesses like yours. "]])
    session = make_session(db, settings, transport, llm, heard, spoken)
    await session._respond()
    assert session.agent_audible()
    await session.interrupt()
    assert transport.cleared >= 1
    assert not session.agent_audible()
    await session.stop("test")


async def test_echo_transcript_after_interrupt_is_dropped(db, make_settings):
    settings = make_settings(voice_backend="gemini", gemini_api_key="g-test")
    transport, spoken, heard = FakeTransport(), [], ["we build websites and mobile apps for local"]
    llm = ScriptLLM([["We build websites and mobile apps for local businesses like yours. "]])
    session = make_session(db, settings, transport, llm, heard, spoken)
    await session._respond()
    n = len(llm.seen)
    await session.interrupt()
    await session._handle_utterance(b"\x00" * 32000, 16000)
    assert len(llm.seen) == n
    await session.stop("test")


def test_silero_ignores_noise():
    ep = SileroEndpointer(in_rate=24000)
    n = 24000 * 3
    noise = struct.pack(f"<{n}h", *(random.randint(-1500, 1500) for _ in range(n)))
    events = []
    for i in range(0, len(noise), 1920):
        events += ep.push(noise[i:i + 1920])
    assert not events and not ep.in_speech


class CountingLLM(FakeLLM):
    def __init__(self):
        super().__init__()
        self.streams = 0

    async def chat_stream(self, messages):
        self.streams += 1
        async for piece in super().chat_stream(messages):
            yield piece


async def wait_for(cond, tries=60):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_early_transcript_and_reply_are_reused_at_end_of_turn(db, make_settings):
    settings = make_settings(openai_api_key="", voice_backend="ollama")
    transport, spoken, llm = FakeTransport(), [], CountingLLM()
    stt_calls = []

    session = make_session(db, settings, transport, llm, ["Yeah we have a site."], spoken)
    real_transcribe = session._transcribe

    async def transcribe(pcm, rate, model):
        stt_calls.append(len(pcm))
        return await real_transcribe(pcm, rate, model)

    session._transcribe = transcribe
    session.messages.append({"role": "assistant", "content": "Hi, it's Alex."})
    audio = b"\x01\x00" * 16000
    await session._on_vad_event(VadEvent("start"))
    await session._on_vad_event(VadEvent("pause", audio))
    await asyncio.sleep(0.05)  # speculation runs during the pause
    await session._on_vad_event(VadEvent("end", audio))
    assert await wait_for(lambda: any(
        isinstance(e, dict) and e.get("type") == "transcript" and e.get("role") == "agent" for e in transport.events
    ))
    assert len(stt_calls) == 1 and llm.streams == 1
    assert spoken
    await session.stop("test")


async def test_mid_sentence_pause_becomes_partial_and_drops_speculation(db, make_settings):
    settings = make_settings(openai_api_key="", voice_backend="ollama")
    transport, spoken, llm = FakeTransport(), [], CountingLLM()
    session = make_session(db, settings, transport, llm, ["We have a"], spoken)
    session.messages.append({"role": "assistant", "content": "Hi, it's Alex."})
    audio = b"\x01\x00" * 8000
    await session._on_vad_event(VadEvent("start"))
    await session._on_vad_event(VadEvent("pause", audio))
    spec = session._spec
    await session._on_vad_event(VadEvent("resume"))
    assert session._spec is None and spec is not None
    assert await wait_for(lambda: any(
        isinstance(e, dict) and e.get("type") == "partial_transcript" for e in transport.events
    ))
    assert not any(isinstance(e, dict) and e.get("type") == "transcript" for e in transport.events)
    await session.stop("test")


async def test_local_session_transcribes_and_speaks(db, make_settings):
    settings = make_settings(openai_api_key="", voice_backend="ollama")
    call = db.create_call(phone="", lead_id=None, campaign_id=None, kind="practice")
    transport = FakeTransport()
    llm = FakeLLM()
    spoken = []

    async def transcribe(pcm, rate, model):
        return "Hello?"

    async def speak(text, _bin, _model):
        spoken.append(text)
        return b"\x00\x01" * 480

    finished = asyncio.Event()

    async def on_finished(_cid):
        finished.set()

    session = LocalVoiceSession(
        settings=settings,
        db=db,
        call=call,
        lead=None,
        campaign=None,
        transport=transport,
        direction="practice",
        on_finished=on_finished,
        llm=llm,
        transcribe=transcribe,
        speak=speak,
    )
    session.endpointer = None  # exercise the energy-VAD fallback with a synthetic tone
    await session.start()
    pcm = base64.b64encode(b"\xff\x7f" * 480 * 10).decode()
    await session.feed_audio(pcm)
    await session.feed_audio(base64.b64encode(b"\x00\x00" * 480 * 50).decode())
    for _ in range(80):
        if any(e.get("type") == "transcript" and e.get("role") == "agent" for e in transport.events if isinstance(e, dict)):
            break
        await asyncio.sleep(0.05)
    await session.stop("test")
    roles = [e.get("role") for e in transport.events if isinstance(e, dict) and e.get("type") == "transcript"]
    assert "prospect" in roles and "agent" in roles
    assert spoken and "Alex" in spoken[0]
    assert transport.audio
