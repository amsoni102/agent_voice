import asyncio
import base64

from agentvoice.audio_util import EnergyVad, resample_s16, split_sentences
from agentvoice.local_voice import LocalVoiceSession
from agentvoice.ollama_llm import chat_tools
from agentvoice.realtime import Transport


def test_auto_backend_uses_ollama_without_openai_key(make_settings):
    s = make_settings(openai_api_key="", voice_backend="auto")
    assert s.uses_ollama and s.voice_available
    s2 = make_settings(openai_api_key="sk-test", voice_backend="auto")
    assert not s2.uses_ollama
    s3 = make_settings(openai_api_key="sk-test", voice_backend="ollama")
    assert s3.uses_ollama


def test_chat_tools_match_playbook_tools():
    names = {t["function"]["name"] for t in chat_tools()}
    assert {"book_meeting", "end_call", "add_to_do_not_call", "record_lead_info"} <= names


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

    async def chat(self, messages):
        self.calls += 1
        user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        if self.calls == 1:
            return "Hey, it's Alex with Stellar Technologies.", []
        if "not interested" in user.lower():
            return "No problem.", [{"name": "end_call", "arguments": {"reason": "not interested"}}]
        return "Got it.", []


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
