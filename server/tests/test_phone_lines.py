from __future__ import annotations

import asyncio
import audioop
import base64
import fractions
import hashlib
import hmac
import json
import math
import re
import socket
import struct
import time

import pytest

from agentvoice.calls import CallManager
from agentvoice.phone_bridge import PhoneBridge
from agentvoice.sip import SipConfig, SipMessage, SipUA, _parse_challenge, build_sdp, parse_message, parse_sdp, tag_of
from agentvoice.whatsapp import call_events, verify_signature


def tone(rate: int, ms: int, freq: float = 440.0, amp: int = 8000) -> bytes:
    n = rate * ms // 1000
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))


class FakeSession:
    def __init__(self) -> None:
        self.audio = bytearray()
        self.marks: list[str] = []
        self.started = False
        self.stopped = ""

    async def feed_audio(self, b64: str) -> None:
        self.audio.extend(base64.b64decode(b64))

    async def on_mark(self, name: str) -> None:
        self.marks.append(name)

    async def start(self) -> None:
        self.started = True

    async def stop(self, reason: str = "") -> None:
        self.stopped = reason or "stopped"


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ bridge
async def test_bridge_resamples_paces_and_fires_marks_after_playback():
    hung = []

    async def hang() -> None:
        hung.append(True)

    bridge = PhoneBridge(8000, hang)
    session = FakeSession()
    bridge.session = session
    await bridge.send_audio(base64.b64encode(tone(24000, 100)).decode())
    await bridge.mark("hangup")
    frames = [bridge.next_frame() for _ in range(4)]
    assert all(len(f) == 320 for f in frames)  # 20 ms at 8 kHz
    await asyncio.sleep(0)
    assert session.marks == []  # 100 ms queued, only 80 ms played
    bridge.next_frame()
    bridge.next_frame()
    await asyncio.sleep(0)
    assert session.marks == ["hangup"]
    assert bridge.next_frame() == b"\x00" * 320

    await bridge.receive(tone(8000, 20))
    assert len(session.audio) == pytest.approx(960, abs=8)  # back to 24 kHz

    await bridge.hangup()
    await bridge.hangup()
    assert hung == [True]


async def test_bridge_clear_drops_queued_audio_and_releases_marks():
    async def hang() -> None:
        pass

    bridge = PhoneBridge(8000, hang)
    session = FakeSession()
    bridge.session = session
    await bridge.send_audio(base64.b64encode(tone(24000, 1000)).decode())
    await bridge.mark("audio-1")
    await bridge.clear()
    await asyncio.sleep(0)
    assert session.marks == ["audio-1"]
    assert bridge.next_frame() == b"\x00" * 320


# --------------------------------------------------------------------- SIP
class FakeTrunk(asyncio.DatagramProtocol):
    """Plays the Zadarma side: registrar + proxy on one UDP socket."""

    def __init__(self, user: str, password: str):
        self.user, self.password = user, password
        self.inbox: asyncio.Queue[tuple[SipMessage, tuple[str, int]]] = asyncio.Queue()
        self.transport: asyncio.DatagramTransport | None = None
        self.helper = SipUA(SipConfig(username="x", password="x"))

    def connection_made(self, transport):  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data, addr):  # type: ignore[override]
        msg = parse_message(data)
        if msg is not None:
            self.inbox.put_nowait((msg, addr))

    async def expect(self, pred, timeout: float = 5.0) -> tuple[SipMessage, tuple[str, int]]:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            msg, addr = await asyncio.wait_for(self.inbox.get(), end - loop.time())
            if pred(msg):
                return msg, addr

    def reply(self, req: SipMessage, code: int, reason: str, addr, **kw) -> SipMessage:
        resp = self.helper.build_response(req, code, reason, **kw)
        self.transport.sendto(resp.encode(), addr)
        return resp

    def check_digest(self, header: str, method: str) -> bool:
        p = _parse_challenge(header)
        ha1 = hashlib.md5(f"{self.user}:{p['realm']}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{p['uri']}".encode()).hexdigest()
        if p.get("qop") == "auth":
            want = hashlib.md5(f"{ha1}:{p['nonce']}:{p['nc']}:{p['cnonce']}:auth:{ha2}".encode()).hexdigest()
        else:
            want = hashlib.md5(f"{ha1}:{p['nonce']}:{ha2}".encode()).hexdigest()
        return p["response"] == want and p["username"] == self.user


class RtpPeer(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.packets: list[bytes] = []
        self.sources: set[tuple[str, int]] = set()
        self.transport = None

    def connection_made(self, transport):  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data, addr):  # type: ignore[override]
        self.packets.append(data)
        self.sources.add(addr)


@pytest.fixture
async def trunk_and_ua():
    loop = asyncio.get_running_loop()
    trunk_t, trunk = await loop.create_datagram_endpoint(lambda: FakeTrunk("12345", "s3cret"), local_addr=("127.0.0.1", 0))
    port = trunk_t.get_extra_info("sockname")[1]
    incoming: asyncio.Queue = asyncio.Queue()

    async def on_incoming(call) -> None:
        await incoming.put(call)

    ua = SipUA(
        SipConfig(username="12345", password="s3cret", server="127.0.0.1", port=port,
                  local_port=free_udp_port(), rtp_ports=(31000, 31999)),
        on_incoming=on_incoming,
    )
    ua_task = asyncio.create_task(ua.start())

    # REGISTER: challenge, then accept and tell the UA its NAT address.
    first, addr = await trunk.expect(lambda m: m.method == "REGISTER")
    assert "authorization" not in dict(first.headers)
    trunk.reply(first, 401, "Unauthorized", addr,
                extra=[("www-authenticate", 'Digest realm="sip.zadarma.com", nonce="abc123", qop="auth", algorithm=MD5')])
    second, addr = await trunk.expect(lambda m: m.method == "REGISTER")
    assert trunk.check_digest(second.get("authorization"), "REGISTER")
    ok = trunk.helper.build_response(second, 200, "OK", extra=[("contact", second.get("contact") + ";expires=300")])
    ok.headers = [(k, f"{v};received=127.0.0.1;rport={addr[1]}" if k == "via" else v) for k, v in ok.headers]
    trunk.transport.sendto(ok.encode(), addr)
    await ua_task
    for _ in range(100):
        if ua.registered:
            break
        await asyncio.sleep(0.02)
    assert ua.registered
    yield trunk, ua, incoming, addr
    for task in ua._tasks:
        task.cancel()
    ua.transport.close()
    trunk_t.close()


async def test_sip_registers_with_digest_and_answers_inbound_call(trunk_and_ua):
    trunk, ua, incoming, ua_addr = trunk_and_ua
    loop = asyncio.get_running_loop()
    rtp_t, rtp_peer = await loop.create_datagram_endpoint(RtpPeer, local_addr=("127.0.0.1", 0))
    rtp_port = rtp_t.get_extra_info("sockname")[1]

    invite = SipMessage("INVITE sip:12345@127.0.0.1 SIP/2.0")
    invite.headers = [
        ("via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKinv1"),
        ("from", "<sip:+48221234567@sip.zadarma.com>;tag=caller1"),
        ("to", "<sip:12345@sip.zadarma.com>"),
        ("call-id", "inbound-call-1"),
        ("cseq", "1 INVITE"),
        ("contact", "<sip:+48221234567@127.0.0.1:5060>"),
        ("content-type", "application/sdp"),
    ]
    invite.body = build_sdp("127.0.0.1", rtp_port, [8, 0], 1)
    trunk.transport.sendto(invite.encode(), ua_addr)

    call = await asyncio.wait_for(incoming.get(), 3)
    assert call.remote_number == "+48221234567"
    heard = bytearray()

    async def on_audio(pcm: bytes) -> None:
        heard.extend(pcm)

    call.on_audio = on_audio
    agent_voice = tone(8000, 20)
    call.audio_source = lambda: agent_voice
    await call.answer()

    await trunk.expect(lambda m: m.status == 100)
    await trunk.expect(lambda m: m.status == 180)
    ok, _ = await trunk.expect(lambda m: m.status == 200 and m.cseq[1] == "INVITE")
    assert tag_of(ok.get("to"))
    ip, port, pts = parse_sdp(ok.body)
    assert pts[0] == 8 and port

    ack = SipMessage("ACK sip:12345@127.0.0.1 SIP/2.0")
    ack.headers = [("via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKack1"), ("from", invite.get("from")),
                   ("to", ok.get("to")), ("call-id", "inbound-call-1"), ("cseq", "1 ACK")]
    trunk.transport.sendto(ack.encode(), ua_addr)

    # Agent audio arrives as PCMA RTP every 20 ms.
    await asyncio.sleep(0.3)
    assert len(rtp_peer.packets) >= 8
    pkt = rtp_peer.packets[-1]
    assert pkt[1] & 0x7F == 8
    assert audioop.rms(audioop.alaw2lin(pkt[12:], 2), 2) > 1000

    # Caller audio reaches the agent.
    for seq in range(5):
        payload = audioop.lin2alaw(tone(8000, 20, 300), 2)
        rtp_t.sendto(struct.pack("!BBHII", 0x80, 8, seq, seq * 160, 99) + payload, ("127.0.0.1", port))
    await asyncio.sleep(0.1)
    assert len(heard) == 5 * 320

    bye = SipMessage("BYE sip:12345@127.0.0.1 SIP/2.0")
    bye.headers = [("via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKbye1"), ("from", invite.get("from")),
                   ("to", ok.get("to")), ("call-id", "inbound-call-1"), ("cseq", "2 BYE")]
    trunk.transport.sendto(bye.encode(), ua_addr)
    await trunk.expect(lambda m: m.status == 200 and m.cseq[1] == "BYE")
    assert await asyncio.wait_for(call.ended, 2) == "remote hangup"
    rtp_t.close()


async def test_sip_outbound_call_authenticates_and_hangs_up(trunk_and_ua):
    trunk, ua, _, ua_addr = trunk_and_ua
    loop = asyncio.get_running_loop()
    rtp_t, rtp_peer = await loop.create_datagram_endpoint(RtpPeer, local_addr=("127.0.0.1", 0))
    rtp_port = rtp_t.get_extra_info("sockname")[1]
    rang = []
    dialing = asyncio.create_task(ua.call("+1 (302) 555-0100", on_ringing=lambda: rang.append(True)))

    inv1, addr = await trunk.expect(lambda m: m.method == "INVITE")
    assert inv1.uri == "sip:13025550100@127.0.0.1"
    trunk.reply(inv1, 407, "Proxy Authentication Required", addr, to_tag="t1",
                extra=[("proxy-authenticate", 'Digest realm="sip.zadarma.com", nonce="n2"')])
    await trunk.expect(lambda m: m.method == "ACK" and m.cseq[0] == inv1.cseq[0])
    inv2, addr = await trunk.expect(lambda m: m.method == "INVITE")
    assert trunk.check_digest(inv2.get("proxy-authorization"), "INVITE")
    trunk.reply(inv2, 180, "Ringing", addr, to_tag="callee")
    trunk.reply(inv2, 200, "OK", addr, to_tag="callee",
                extra=[("contact", "<sip:13025550100@127.0.0.1:5060>"), ("content-type", "application/sdp")],
                body=build_sdp("127.0.0.1", rtp_port, [0], 7))
    ack, _ = await trunk.expect(lambda m: m.method == "ACK")
    assert ack.uri == "sip:13025550100@127.0.0.1:5060"

    call = await asyncio.wait_for(dialing, 3)
    assert rang == [True] and call.state == "answered"
    await asyncio.sleep(0.2)
    assert rtp_peer.packets and rtp_peer.packets[-1][1] & 0x7F == 0  # callee picked PCMU

    hang = asyncio.create_task(call.hangup())
    bye, addr = await trunk.expect(lambda m: m.method == "BYE")
    assert tag_of(bye.get("to")) == "callee"
    trunk.reply(bye, 200, "OK", addr)
    await asyncio.wait_for(hang, 3)
    assert call.state == "ended"
    rtp_t.close()


async def test_sip_outbound_busy_raises(trunk_and_ua):
    from agentvoice.sip import SipCallFailed

    trunk, ua, _, _ = trunk_and_ua
    dialing = asyncio.create_task(ua.call("13025550101"))
    inv, addr = await trunk.expect(lambda m: m.method == "INVITE")
    trunk.reply(inv, 486, "Busy Here", addr, to_tag="b")
    with pytest.raises(SipCallFailed) as exc:
        await asyncio.wait_for(dialing, 3)
    assert exc.value.status == 486
    await trunk.expect(lambda m: m.method == "ACK")


async def test_zadarma_inbound_call_runs_real_agent_session_end_to_end(trunk_and_ua, make_settings, db):
    """Caller dials the Zadarma number: agent greets over RTP, then ends the call with a BYE."""
    from agentvoice.local_voice import LocalVoiceSession
    from agentvoice.zadarma import ZadarmaLine

    from test_local_voice import ScriptLLM

    trunk, ua, _, ua_addr = trunk_and_ua
    settings = make_settings(voice_backend="gemini", gemini_api_key="g-test",
                             zadarma_sip_login="12345", zadarma_sip_password="s3cret")
    manager = CallManager(settings, db)
    line = ZadarmaLine(manager)
    line.ua = ua
    ua.on_incoming = line._incoming
    llm = ScriptLLM([["Hi, this is Sam from Stellar Technologies, thanks for calling. Have a great day! [END_CALL]"]])
    spoken = []

    async def speak(text, _bin, _model):
        spoken.append(text)
        return tone(24000, 300)

    async def transcribe(pcm, rate, model):
        return ""

    def start_line_session(call, transport, direction):
        session = LocalVoiceSession(settings=settings, db=db, call=call, lead=None, campaign=None,
                                    transport=transport, direction=direction, on_finished=manager.on_session_finished,
                                    llm=llm, transcribe=transcribe, speak=speak)
        transport.session = session
        manager.sessions[call["id"]] = session
        return session

    manager.start_line_session = start_line_session  # type: ignore[method-assign]
    manager.summarize_call = lambda call_id: asyncio.sleep(0)  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    rtp_t, rtp_peer = await loop.create_datagram_endpoint(RtpPeer, local_addr=("127.0.0.1", 0))
    invite = SipMessage("INVITE sip:12345@127.0.0.1 SIP/2.0")
    invite.headers = [
        ("via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKe2e"),
        ("from", "<sip:13025550199@sip.zadarma.com>;tag=c9"),
        ("to", "<sip:12345@sip.zadarma.com>"),
        ("call-id", "e2e-call"),
        ("cseq", "1 INVITE"),
        ("contact", "<sip:13025550199@127.0.0.1:5060>"),
        ("content-type", "application/sdp"),
    ]
    invite.body = build_sdp("127.0.0.1", rtp_t.get_extra_info("sockname")[1], [0, 8], 3)
    trunk.transport.sendto(invite.encode(), ua_addr)
    ok, _ = await trunk.expect(lambda m: m.status == 200 and m.cseq[1] == "INVITE")
    ack = SipMessage("ACK sip:12345@127.0.0.1 SIP/2.0")
    ack.headers = [("via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKe2ea"), ("from", invite.get("from")),
                   ("to", ok.get("to")), ("call-id", "e2e-call"), ("cseq", "1 ACK")]
    trunk.transport.sendto(ack.encode(), ua_addr)

    bye, addr = await trunk.expect(lambda m: m.method == "BYE", timeout=10)
    trunk.reply(bye, 200, "OK", addr)
    assert spoken and "Stellar Technologies" in " ".join(spoken)
    loud = [p for p in rtp_peer.packets if audioop.rms(audioop.ulaw2lin(p[12:], 2), 2) > 1000]
    assert len(loud) >= 10, "agent speech never reached the caller"
    for _ in range(50):
        call = db.list_calls(limit=1)[0]
        if call["status"] == "completed":
            break
        await asyncio.sleep(0.05)
    assert call["direction"] == "inbound" and call["phone"] == "+13025550199" and call["status"] == "completed"
    assert any(t["role"] == "agent" for t in db.get_transcript(call["id"]))
    rtp_t.close()


# ---------------------------------------------------------------- WhatsApp
def test_whatsapp_signature_and_event_parsing():
    body = b'{"x":1}'
    sig = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    assert verify_signature("appsecret", body, sig)
    assert not verify_signature("appsecret", body, "sha256=bad")
    payload = {"entry": [{"changes": [
        {"field": "messages", "value": {}},
        {"field": "calls", "value": {
            "calls": [{"id": "wacid.1", "event": "connect"}],
            "statuses": [{"id": "wacid.1", "type": "call", "status": "RINGING"}],
        }},
    ]}]}
    kinds = [e["event"] for e, _ in call_events(payload)]
    assert kinds == ["connect", "status"]


async def test_whatsapp_webhook_verification(make_settings, db):
    from fastapi.testclient import TestClient

    from agentvoice.api import create_app

    app = create_app(make_settings(whatsapp_verify_token="vt", whatsapp_app_secret="as"), db)
    client = TestClient(app)
    ok = client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"})
    assert bad.status_code == 403
    assert client.post("/whatsapp/webhook", content=b"{}", headers={"x-hub-signature-256": "sha256=0"}).status_code == 403


async def test_whatsapp_inbound_call_media_flows_both_ways(make_settings, db):
    import av
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import MediaStreamTrack

    class Tone(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self.pts = 0
            self.t0 = time.monotonic()

        async def recv(self):
            self.pts += 960
            await asyncio.sleep(max(0, self.t0 + self.pts / 48000 - time.monotonic()))
            frame = av.AudioFrame(format="s16", layout="mono", samples=960)
            frame.planes[0].update(tone(48000, 20, 300))
            frame.sample_rate = 48000
            frame.pts = self.pts
            frame.time_base = fractions.Fraction(1, 48000)
            return frame

    settings = make_settings(whatsapp_token="tok", whatsapp_phone_number_id="555", whatsapp_verify_token="vt")
    manager = CallManager(settings, db)
    session = FakeSession()

    def start_line_session(call, transport, direction):
        transport.session = session
        session.direction = direction
        manager.sessions[call["id"]] = session
        return session

    manager.start_line_session = start_line_session  # type: ignore[method-assign]
    meta = RTCPeerConnection()
    meta.addTrack(Tone())
    heard_by_caller = []

    @meta.on("track")
    def on_track(track):
        async def pump():
            while True:
                try:
                    frame = await track.recv()
                except Exception:  # noqa: BLE001
                    return
                heard_by_caller.append(audioop.rms(bytes(frame.planes[0]), 2))
        asyncio.ensure_future(pump())

    await meta.setLocalDescription(await meta.createOffer())
    actions = []

    async def fake_api(body):
        actions.append(body["action"])
        if body["action"] == "pre_accept":
            await meta.setRemoteDescription(RTCSessionDescription(sdp=body["session"]["sdp"], type="answer"))
        return {"success": True}

    manager.whatsapp.api = fake_api
    webhook = {"entry": [{"changes": [{"field": "calls", "value": {
        "metadata": {"phone_number_id": "555"},
        "calls": [{"id": "wacid.T1", "from": "13025550123", "event": "connect",
                   "session": {"sdp_type": "offer", "sdp": meta.localDescription.sdp}}],
    }}]}]}
    await manager.whatsapp.handle_webhook(webhook)
    assert actions == ["pre_accept", "accept"]
    assert session.direction == "inbound"
    wa = manager.whatsapp.calls["wacid.T1"]
    call_row = db.get_call(wa.call_row["id"])
    assert call_row["phone"] == "+13025550123" and call_row["status"] == "in-progress"

    await wa.bridge.send_audio(base64.b64encode(tone(24000, 1500)).decode())
    for _ in range(100):
        if len(session.audio) > 24000 and any(r > 500 for r in heard_by_caller):
            break
        await asyncio.sleep(0.05)
    assert len(session.audio) > 24000, "caller audio did not reach the agent"
    assert audioop.rms(bytes(session.audio[-4800:]), 2) > 500
    assert any(r > 500 for r in heard_by_caller), "agent audio did not reach the caller"

    end = {"entry": [{"changes": [{"field": "calls", "value": {
        "calls": [{"id": "wacid.T1", "event": "terminate", "status": "COMPLETED"}]}}]}]}
    await manager.whatsapp.handle_webhook(end)
    for _ in range(50):
        if session.stopped:
            break
        await asyncio.sleep(0.02)
    assert session.stopped
    assert db.get_call(call_row["id"])["status"] == "completed"
    await meta.close()
    await manager.shutdown()
