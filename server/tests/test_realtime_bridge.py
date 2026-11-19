"""End-to-end test of the voice bridge against a local fake of the OpenAI Realtime API."""

import asyncio
import base64
import json

from websockets.asyncio.server import serve

from agentvoice.realtime import Transport, VoiceSession


class FakeTransport(Transport):
    audio_format = {"type": "audio/pcmu"}
    bytes_per_ms = 8.0

    def __init__(self):
        self.audio: list[str] = []
        self.marks: list[str] = []
        self.cleared = 0
        self.hung_up = False
        self.session: VoiceSession | None = None

    async def send_audio(self, b64_audio):
        self.audio.append(b64_audio)

    async def clear(self):
        self.cleared += 1

    async def mark(self, name):
        self.marks.append(name)
        # Like Twilio: echo the mark once "playback" finishes.
        asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(self.session.on_mark(name)))

    async def hangup(self):
        self.hung_up = True


async def fake_openai(received: list[dict], ws):
    async for raw in ws:
        event = json.loads(raw)
        received.append(event)
        if event["type"] == "input_audio_buffer.append" and sum(
            1 for e in received if e["type"] == "input_audio_buffer.append"
        ) == 1:
            audio = base64.b64encode(b"\xff" * 1600).decode()  # 200 ms of u-law
            for out in (
                {"type": "input_audio_buffer.speech_started"},
                {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Hello?"},
                {"type": "response.created"},
                {"type": "response.output_audio.delta", "item_id": "item_1", "delta": audio},
                {"type": "response.output_audio.done", "item_id": "item_1"},
                {"type": "response.output_audio_transcript.done", "item_id": "item_1",
                 "transcript": "Hey, it's Alex with Stellar Technologies."},
                {"type": "response.function_call_arguments.done", "call_id": "fc_1",
                 "name": "set_outcome", "arguments": json.dumps({"outcome": "not_interested"})},
                {"type": "response.function_call_arguments.done", "call_id": "fc_2",
                 "name": "end_call", "arguments": json.dumps({"reason": "not interested"})},
                {"type": "response.done", "response": {"status": "completed"}},
            ):
                await ws.send(json.dumps(out))


async def test_bridge_conversation_tools_and_hangup(db, make_settings):
    received: list[dict] = []
    async with serve(lambda ws: fake_openai(received, ws), "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        settings = make_settings(realtime_url=f"ws://127.0.0.1:{port}/v1/realtime")
        lead = db.create_lead("+15125550142", business_name="Joe's Tacos", state="TX", consent=True)
        db.update_lead(lead["id"], status="calling")
        call = db.create_call(phone=lead["phone"], lead_id=lead["id"], campaign_id=None)
        finished = asyncio.Event()

        async def on_finished(call_id):
            finished.set()

        transport = FakeTransport()
        session = VoiceSession(
            settings=settings, db=db, call=call, lead=db.get_lead(lead["id"]), campaign=None,
            transport=transport, direction="outbound", on_finished=on_finished,
        )
        transport.session = session
        await session.start()
        await session.feed_audio(base64.b64encode(b"\x7f" * 160).decode())
        await asyncio.wait_for(finished.wait(), timeout=5)

    update = received[0]
    assert update["type"] == "session.update"
    assert update["session"]["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert "Joe's Tacos" in update["session"]["instructions"]
    assert {t["name"] for t in update["session"]["tools"]} >= {"book_meeting", "end_call", "add_to_do_not_call"}

    outputs = [e for e in received if e["type"] == "conversation.item.create"]
    assert {o["item"]["call_id"] for o in outputs} == {"fc_1", "fc_2"}
    # end_call was requested, so no follow-up response should be generated.
    assert not any(e["type"] == "response.create" for e in received if received.index(e) > received.index(outputs[-1]))

    assert transport.audio and transport.hung_up
    assert "hangup" in transport.marks
    transcript = db.get_transcript(call["id"])
    assert [t["role"] for t in transcript if t["role"] != "tool"] == ["prospect", "agent"]
    assert db.get_call(call["id"])["outcome"] == "not_interested"
    assert db.get_lead(lead["id"])["status"] == "not_interested"


async def test_barge_in_truncates_playback(db, make_settings):
    received: list[dict] = []

    async def server_handler(ws):
        async for raw in ws:
            event = json.loads(raw)
            received.append(event)
            if event["type"] == "input_audio_buffer.append":
                audio = base64.b64encode(b"\xff" * 16000).decode()  # 2 s of audio
                await ws.send(json.dumps({"type": "response.created"}))
                await ws.send(json.dumps({"type": "response.output_audio.delta", "item_id": "it", "delta": audio}))
                await asyncio.sleep(0.3)
                await ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))

    async with serve(server_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        settings = make_settings(realtime_url=f"ws://127.0.0.1:{port}/v1/realtime")
        call = db.create_call(phone="", lead_id=None, campaign_id=None, kind="practice")
        transport = FakeTransport()
        session = VoiceSession(settings=settings, db=db, call=call, lead=None, campaign=None,
                               transport=transport, direction="practice")
        transport.session = session
        await session.start()
        await session.feed_audio(base64.b64encode(b"\x7f" * 160).decode())
        for _ in range(50):
            if any(e["type"] == "conversation.item.truncate" for e in received):
                break
            await asyncio.sleep(0.05)
        await session.stop("test done")

    truncate = next(e for e in received if e["type"] == "conversation.item.truncate")
    assert truncate["item_id"] == "it"
    assert 200 <= truncate["audio_end_ms"] < 2000
    assert transport.cleared == 1
