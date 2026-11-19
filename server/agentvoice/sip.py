"""Minimal SIP user agent (UDP) with G.711 RTP, enough to register to a SIP trunk such as Zadarma,
answer inbound calls and place outbound calls."""

from __future__ import annotations

import asyncio
import audioop
import hashlib
import logging
import random
import re
import secrets
import socket
import struct
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("agentvoice.sip")

USER_AGENT = "AgentVoice/1.0"
CODECS = {0: "PCMU", 8: "PCMA"}
DTMF_PT = 101
FRAME_SAMPLES = 160  # 20 ms at 8 kHz
T1 = 0.5

_COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length",
            "c": "content-type", "k": "supported"}


# ----------------------------------------------------------------- messages
@dataclass
class SipMessage:
    start: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: str = ""

    @property
    def is_request(self) -> bool:
        return not self.start.startswith("SIP/2.0")

    @property
    def method(self) -> str:
        return self.start.split(" ", 1)[0] if self.is_request else ""

    @property
    def status(self) -> int:
        return 0 if self.is_request else int(self.start.split(" ", 2)[1])

    @property
    def uri(self) -> str:
        return self.start.split(" ")[1] if self.is_request else ""

    def get(self, name: str, default: str = "") -> str:
        name = name.lower()
        for key, value in self.headers:
            if key == name:
                return value
        return default

    def get_all(self, name: str) -> list[str]:
        name = name.lower()
        out: list[str] = []
        for key, value in self.headers:
            if key == name:
                out.extend(_split_header(value) if name in ("via", "record-route", "route") else [value])
        return out

    @property
    def cseq(self) -> tuple[int, str]:
        num, method = self.get("cseq", "0 X").split()
        return int(num), method

    def encode(self) -> bytes:
        body = self.body.encode()
        lines = [self.start]
        for key, value in self.headers:
            if key != "content-length":
                lines.append(f"{_canonical(key)}: {value}")
        lines.append(f"Content-Length: {len(body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _canonical(name: str) -> str:
    special = {"call-id": "Call-ID", "cseq": "CSeq", "www-authenticate": "WWW-Authenticate"}
    return special.get(name, "-".join(p.capitalize() for p in name.split("-")))


def _split_header(value: str) -> list[str]:
    parts, depth, cur, quoted = [], 0, "", False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        if not quoted:
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append(cur.strip())
                cur = ""
                continue
        cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def parse_message(data: bytes) -> SipMessage | None:
    try:
        text = data.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    if not lines or not lines[0].strip():
        return None
    msg = SipMessage(lines[0].strip())
    for line in lines[1:]:
        if line[:1] in (" ", "\t") and msg.headers:
            key, value = msg.headers[-1]
            msg.headers[-1] = (key, value + " " + line.strip())
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        msg.headers.append((_COMPACT.get(key, key), value.strip()))
    length = msg.get("content-length")
    msg.body = body[: int(length)] if length.isdigit() else body
    return msg


def tag_of(header: str) -> str:
    m = re.search(r";\s*tag=([^;>\s]+)", header)
    return m.group(1) if m else ""


def uri_of(header: str) -> str:
    m = re.search(r"<([^>]+)>", header)
    return m.group(1) if m else header.split(";")[0].strip()


def user_of(header: str) -> str:
    m = re.match(r"sips?:([^@;>]+)@", uri_of(header))
    return m.group(1) if m else ""


def _host_port(uri: str, default_port: int = 5060) -> tuple[str, int]:
    m = re.match(r"sips?:(?:[^@]+@)?([^:;>]+)(?::(\d+))?", uri)
    if not m:
        raise ValueError(f"bad SIP uri {uri}")
    return m.group(1), int(m.group(2) or default_port)


# ------------------------------------------------------------------- digest
def _parse_challenge(header: str) -> dict[str, str]:
    _, _, rest = header.partition(" ")
    return {k.lower(): v1 or v2 for k, v1, v2 in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', rest)}


def digest_authorization(challenge: str, method: str, uri: str, username: str, password: str, nc: int = 1) -> str:
    ch = _parse_challenge(challenge)
    realm, nonce = ch.get("realm", ""), ch.get("nonce", "")
    algorithm = ch.get("algorithm", "MD5")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    qops = [q.strip() for q in ch.get("qop", "").split(",") if q.strip()]
    if "auth" in qops:
        cnonce = secrets.token_hex(8)
        ncs = f"{nc:08x}"
        resp = hashlib.md5(f"{ha1}:{nonce}:{ncs}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        parts += [f'response="{resp}"', "qop=auth", f"nc={ncs}", f'cnonce="{cnonce}"']
    else:
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        parts.append(f'response="{resp}"')
    parts.append(f"algorithm={algorithm}")
    if ch.get("opaque"):
        parts.append(f'opaque="{ch["opaque"]}"')
    return "Digest " + ", ".join(parts)


# ---------------------------------------------------------------------- SDP
def build_sdp(ip: str, port: int, codecs: list[int], session_id: int) -> str:
    pts = " ".join(str(c) for c in codecs + [DTMF_PT])
    lines = [
        "v=0",
        f"o=agentvoice {session_id} {session_id} IN IP4 {ip}",
        "s=AgentVoice",
        f"c=IN IP4 {ip}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {pts}",
    ]
    lines += [f"a=rtpmap:{c} {CODECS[c]}/8000" for c in codecs]
    lines += [f"a=rtpmap:{DTMF_PT} telephone-event/8000", f"a=fmtp:{DTMF_PT} 0-16", "a=ptime:20", "a=sendrecv"]
    return "\r\n".join(lines) + "\r\n"


def parse_sdp(sdp: str) -> tuple[str, int, list[int]]:
    ip, port, pts = "", 0, []
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
        elif line.startswith("m=audio "):
            fields = line.split()
            port = int(fields[1])
            pts = [int(p) for p in fields[3:] if p.isdigit()]
    return ip, port, pts


# ---------------------------------------------------------------------- RTP
class RtpStream(asyncio.DatagramProtocol):
    """G.711 RTP: sends a frame every 20 ms from `source()`, delivers decoded caller audio to `sink`."""

    def __init__(self, payload_type: int, source: Callable[[], bytes], sink: Callable[[bytes], Awaitable[None]]):
        self.pt = payload_type
        self.source = source
        self.sink = sink
        self.remote: tuple[str, int] | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self.seq = random.randint(0, 0xFFFF)
        self.ts = random.randint(0, 0xFFFFFFFF)
        self.ssrc = random.randint(1, 0xFFFFFFFF)
        self.received = 0
        self._latched = False
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._sink_task: asyncio.Task[None] | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 12 or data[0] >> 6 != 2:
            return
        if not self._latched:
            self.remote = addr  # symmetric RTP: answer where the media really comes from (NAT)
            self._latched = True
        cc = data[0] & 0x0F
        offset = 12 + 4 * cc
        if data[0] & 0x10 and len(data) >= offset + 4:
            offset += 4 + 4 * struct.unpack("!H", data[offset + 2 : offset + 4])[0]
        pt = data[1] & 0x7F
        payload = data[offset:]
        if data[0] & 0x20 and payload:
            payload = payload[: -payload[-1]]
        if pt == 0:
            pcm = audioop.ulaw2lin(payload, 2)
        elif pt == 8:
            pcm = audioop.alaw2lin(payload, 2)
        else:
            return
        self.received += 1
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            pass

    def start(self, remote: tuple[str, int]) -> None:
        if self.remote is None:
            self.remote = remote
        self._task = asyncio.create_task(self._send_loop(), name="rtp-send")
        self._sink_task = asyncio.create_task(self._sink_loop(), name="rtp-sink")

    async def _sink_loop(self) -> None:
        while True:
            pcm = await self._queue.get()
            try:
                await self.sink(pcm)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("rtp sink failed")

    async def _send_loop(self) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        n = 0
        marker = 0x80
        while True:
            pcm = self.source()
            payload = audioop.lin2alaw(pcm, 2) if self.pt == 8 else audioop.lin2ulaw(pcm, 2)
            header = struct.pack("!BBHII", 0x80, marker | self.pt, self.seq, self.ts, self.ssrc)
            marker = 0
            if self.transport is not None and self.remote is not None:
                self.transport.sendto(header + payload, self.remote)
            self.seq = (self.seq + 1) & 0xFFFF
            self.ts = (self.ts + FRAME_SAMPLES) & 0xFFFFFFFF
            n += 1
            delay = start + n * 0.02 - loop.time()
            if delay < -0.2:  # fell far behind (event loop stall): resync instead of bursting
                start, n = loop.time(), 0
                delay = 0
            await asyncio.sleep(max(0.0, delay))

    def close(self) -> None:
        for task in (self._task, self._sink_task):
            if task:
                task.cancel()
        if self.transport:
            self.transport.close()


# --------------------------------------------------------------------- calls
@dataclass
class SipConfig:
    username: str
    password: str
    server: str = "sip.zadarma.com"
    port: int = 5060
    domain: str = ""
    local_port: int = 5062
    public_ip: str = ""
    rtp_ports: tuple[int, int] = (16000, 16999)
    expires: int = 300
    codecs: tuple[int, ...] = (8, 0)

    @property
    def realm_domain(self) -> str:
        return self.domain or self.server


class SipCall:
    def __init__(self, ua: "SipUA", call_id: str, direction: str, remote_number: str):
        self.ua = ua
        self.call_id = call_id
        self.direction = direction  # "inbound" | "outbound"
        self.remote_number = remote_number
        self.local_tag = secrets.token_hex(5)
        self.remote_tag = ""
        self.local_uri = ""
        self.remote_uri = ""
        self.remote_target = ""
        self.route_set: list[str] = []
        self.cseq = random.randint(1, 1000)
        self.rtp: RtpStream | None = None
        self.rtp_port = 0
        self.state = "init"  # init, ringing, answered, ended
        self.answered = asyncio.get_event_loop().create_future()
        self.ended = asyncio.get_event_loop().create_future()
        self.end_reason = ""
        self.invite: SipMessage | None = None
        self.invite_addr: tuple[str, int] | None = None
        self.on_audio: Callable[[bytes], Awaitable[None]] | None = None
        self.audio_source: Callable[[], bytes] = lambda: b"\x00" * (FRAME_SAMPLES * 2)
        self._ok: bytes | None = None
        self._acked = asyncio.Event()

    async def _sink(self, pcm: bytes) -> None:
        if self.on_audio is not None:
            await self.on_audio(pcm)

    async def _open_rtp(self, pt: int) -> None:
        loop = asyncio.get_running_loop()
        lo, hi = self.ua.config.rtp_ports
        last_exc: Exception | None = None
        for _ in range(50):
            port = random.randrange(lo, hi, 2)
            try:
                _, proto = await loop.create_datagram_endpoint(
                    lambda: RtpStream(pt, lambda: self.audio_source(), self._sink),
                    local_addr=("0.0.0.0", port),
                )
            except OSError as exc:
                last_exc = exc
                continue
            self.rtp, self.rtp_port = proto, port
            return
        raise RuntimeError(f"no free RTP port: {last_exc}")

    def _choose_codec(self, offered: list[int]) -> int:
        for pt in self.ua.config.codecs:
            if pt in offered:
                return pt
        return self.ua.config.codecs[0]

    async def answer(self) -> None:
        """Accept an inbound INVITE: 180, then 200 with SDP; media starts immediately."""
        assert self.invite is not None and self.invite_addr is not None
        ip, port, offered = parse_sdp(self.invite.body)
        pt = self._choose_codec(offered)
        await self._open_rtp(pt)
        self.ua.send_response(self.invite, 180, "Ringing", self.invite_addr, to_tag=self.local_tag)
        sdp = build_sdp(self.ua.media_ip, self.rtp_port, [pt], random.randint(1, 10**9))
        ok = self.ua.build_response(
            self.invite, 200, "OK", to_tag=self.local_tag,
            extra=[("contact", self.ua.contact()), ("content-type", "application/sdp"),
                   ("allow", "INVITE, ACK, CANCEL, BYE, OPTIONS, UPDATE")],
            body=sdp,
        )
        self._ok = ok.encode()
        self.state = "answered"
        assert self.rtp is not None
        self.rtp.start((ip, port))
        asyncio.create_task(self._retransmit_ok())
        if not self.answered.done():
            self.answered.set_result(True)

    async def _retransmit_ok(self) -> None:
        interval = T1
        waited = 0.0
        while not self._acked.is_set() and self.state == "answered" and waited < 32:
            self.ua.sendto(self._ok or b"", self.invite_addr)  # type: ignore[arg-type]
            try:
                await asyncio.wait_for(self._acked.wait(), interval)
            except asyncio.TimeoutError:
                waited += interval
                interval = min(interval * 2, 4.0)

    def reject(self, code: int = 486, reason: str = "Busy Here") -> None:
        if self.invite is not None and self.invite_addr is not None:
            self.ua.send_response(self.invite, code, reason, self.invite_addr, to_tag=self.local_tag)
        self._finish(f"rejected {code}")

    async def hangup(self) -> None:
        if self.state == "ended":
            return
        if self.state == "answered":
            self.cseq += 1
            req = self.ua.in_dialog_request(self, "BYE")
            try:
                await self.ua.transaction(req, timeout=4)
            except Exception:  # noqa: BLE001
                pass
        elif self.direction == "outbound" and self.invite is not None:
            cancel = self.ua.cancel_for(self.invite)
            try:
                await self.ua.transaction(cancel, timeout=4)
            except Exception:  # noqa: BLE001
                pass
        elif self.direction == "inbound" and self.invite is not None:
            self.reject(480, "Temporarily Unavailable")
            return
        self._finish("local hangup")

    def _finish(self, reason: str) -> None:
        if self.state == "ended":
            return
        self.state = "ended"
        self.end_reason = reason
        if self.rtp:
            self.rtp.close()
        if not self.answered.done():
            self.answered.set_result(False)
        if not self.ended.done():
            self.ended.set_result(reason)
        self.ua.calls.pop(self.call_id, None)
        log.info("sip call %s ended: %s", self.call_id[:12], reason)


class SipUA(asyncio.DatagramProtocol):
    def __init__(self, config: SipConfig, on_incoming: Callable[[SipCall], Awaitable[None]] | None = None):
        self.config = config
        self.on_incoming = on_incoming
        self.transport: asyncio.DatagramTransport | None = None
        self.server_addr: tuple[str, int] | None = None
        self.local_ip = ""
        self.public_addr: tuple[str, int] | None = None
        self.calls: dict[str, SipCall] = {}
        self.registered = False
        self.last_error = ""
        self._pending: dict[tuple[str, int, str], asyncio.Queue[SipMessage]] = {}
        self._reg_call_id = secrets.token_hex(12)
        self._reg_cseq = 0
        self._reg_tag = secrets.token_hex(5)
        self._tasks: list[asyncio.Task[Any]] = []

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(self.config.server, self.config.port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        self.server_addr = infos[0][4][:2]
        await loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", self.config.local_port))
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(self.server_addr)
            self.local_ip = probe.getsockname()[0]
        finally:
            probe.close()
        self._tasks.append(asyncio.create_task(self._register_loop(), name="sip-register"))
        self._tasks.append(asyncio.create_task(self._keepalive(), name="sip-keepalive"))

    async def stop(self) -> None:
        for call in list(self.calls.values()):
            await call.hangup()
        if self.registered:
            try:
                await self.register(expires=0)
            except Exception:  # noqa: BLE001
                pass
        for task in self._tasks:
            task.cancel()
        if self.transport:
            self.transport.close()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    # --------------------------------------------------------- addressing
    @property
    def contact_host(self) -> tuple[str, int]:
        if self.public_addr:
            return self.public_addr
        return self.local_ip, self.config.local_port

    @property
    def media_ip(self) -> str:
        return self.config.public_ip or (self.public_addr[0] if self.public_addr else self.local_ip)

    def contact(self) -> str:
        host, port = self.contact_host
        return f"<sip:{self.config.username}@{host}:{port}>"

    def aor(self) -> str:
        return f"sip:{self.config.username}@{self.config.realm_domain}"

    def via(self, branch: str) -> str:
        return f"SIP/2.0/UDP {self.local_ip}:{self.config.local_port};branch={branch};rport"

    def sendto(self, data: bytes, addr: tuple[str, int] | None = None) -> None:
        if self.transport is not None:
            self.transport.sendto(data, addr or self.server_addr)

    # --------------------------------------------------------- transactions
    def new_request(self, method: str, uri: str, *, call_id: str, cseq: int, from_: str, to: str,
                    extra: list[tuple[str, str]] | None = None, body: str = "") -> SipMessage:
        msg = SipMessage(f"{method} {uri} SIP/2.0")
        msg.headers = [
            ("via", self.via("z9hG4bK" + secrets.token_hex(8))),
            ("max-forwards", "70"),
            ("from", from_),
            ("to", to),
            ("call-id", call_id),
            ("cseq", f"{cseq} {method}"),
            ("user-agent", USER_AGENT),
        ]
        msg.headers += extra or []
        msg.body = body
        return msg

    async def transaction(self, req: SipMessage, timeout: float = 32.0, addr: tuple[str, int] | None = None,
                          provisional: Callable[[SipMessage], None] | None = None) -> SipMessage:
        """Send a request (retransmitting per RFC 3261 timer A/E) and return the final response."""
        num, method = req.cseq
        key = (req.get("call-id"), num, method)
        queue: asyncio.Queue[SipMessage] = asyncio.Queue()
        self._pending[key] = queue
        data = req.encode()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        interval = T1
        got_provisional = False
        try:
            self.sendto(data, addr)
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError(f"{method} timed out")
                wait = remaining if got_provisional and method == "INVITE" else min(interval, remaining)
                try:
                    resp = await asyncio.wait_for(queue.get(), wait)
                except asyncio.TimeoutError:
                    if not got_provisional or method != "INVITE":
                        self.sendto(data, addr)
                        interval = min(interval * 2, 4.0)
                    continue
                if resp.status < 200:
                    got_provisional = True
                    interval = 4.0
                    if provisional:
                        provisional(resp)
                    continue
                return resp
        finally:
            self._pending.pop(key, None)

    def build_response(self, req: SipMessage, code: int, reason: str, *, to_tag: str = "",
                       extra: list[tuple[str, str]] | None = None, body: str = "") -> SipMessage:
        msg = SipMessage(f"SIP/2.0 {code} {reason}")
        for key, value in req.headers:
            if key in ("via", "from", "call-id", "cseq", "record-route"):
                msg.headers.append((key, value))
            elif key == "to":
                if to_tag and not tag_of(value):
                    value = f"{value};tag={to_tag}"
                msg.headers.append((key, value))
        msg.headers.append(("user-agent", USER_AGENT))
        msg.headers += extra or []
        msg.body = body
        return msg

    def send_response(self, req: SipMessage, code: int, reason: str, addr: tuple[str, int], **kw: Any) -> None:
        self.sendto(self.build_response(req, code, reason, **kw).encode(), addr)

    def in_dialog_request(self, call: SipCall, method: str, body: str = "",
                          extra: list[tuple[str, str]] | None = None) -> SipMessage:
        local = f"<{call.local_uri}>;tag={call.local_tag}"
        remote = f"<{call.remote_uri}>" + (f";tag={call.remote_tag}" if call.remote_tag else "")
        headers = [("route", r) for r in call.route_set]
        headers.append(("contact", self.contact()))
        return self.new_request(method, call.remote_target or call.remote_uri, call_id=call.call_id,
                                cseq=call.cseq, from_=local, to=remote, extra=headers + (extra or []), body=body)

    def cancel_for(self, invite: SipMessage) -> SipMessage:
        msg = SipMessage(f"CANCEL {invite.uri} SIP/2.0")
        for key, value in invite.headers:
            if key in ("via", "from", "to", "call-id", "max-forwards", "route"):
                msg.headers.append((key, value))
        msg.headers.append(("cseq", f"{invite.cseq[0]} CANCEL"))
        return msg

    def _ack(self, invite: SipMessage, resp: SipMessage, uri: str | None = None, routes: list[str] | None = None) -> None:
        """ACK for a final response (non-2xx reuses the INVITE branch; 2xx is a new in-dialog request)."""
        msg = SipMessage(f"ACK {uri or invite.uri} SIP/2.0")
        if uri is None:
            msg.headers.append(("via", invite.get("via")))
        else:
            msg.headers.append(("via", self.via("z9hG4bK" + secrets.token_hex(8))))
        msg.headers += [("max-forwards", "70"), ("from", invite.get("from")), ("to", resp.get("to")),
                        ("call-id", invite.get("call-id")), ("cseq", f"{invite.cseq[0]} ACK")]
        msg.headers += [("route", r) for r in (routes or [])]
        self.sendto(msg.encode())

    # ------------------------------------------------------------- register
    async def register(self, expires: int | None = None) -> SipMessage:
        expires = self.config.expires if expires is None else expires
        auth: list[tuple[str, str]] = []
        for _ in range(3):
            self._reg_cseq += 1
            aor = self.aor()
            req = self.new_request(
                "REGISTER", f"sip:{self.config.realm_domain}", call_id=self._reg_call_id, cseq=self._reg_cseq,
                from_=f"<{aor}>;tag={self._reg_tag}", to=f"<{aor}>",
                extra=[("contact", self.contact()), ("expires", str(expires)),
                       ("allow", "INVITE, ACK, CANCEL, BYE, OPTIONS, UPDATE")] + auth,
            )
            resp = await self.transaction(req, timeout=10)
            self._learn_public_addr(resp)
            if resp.status in (401, 407):
                header = "www-authenticate" if resp.status == 401 else "proxy-authenticate"
                name = "authorization" if resp.status == 401 else "proxy-authorization"
                value = digest_authorization(resp.get(header), "REGISTER", f"sip:{self.config.realm_domain}",
                                             self.config.username, self.config.password)
                auth = [(name, value)]
                continue
            return resp
        return resp

    def _learn_public_addr(self, resp: SipMessage) -> bool:
        via = resp.get("via")
        received = re.search(r"received=([\d.]+)", via)
        rport = re.search(r"rport=(\d+)", via)
        if not received and not rport:
            return False
        addr = (received.group(1) if received else self.local_ip,
                int(rport.group(1)) if rport else self.config.local_port)
        changed = addr != self.contact_host
        self.public_addr = addr
        return changed

    async def _register_loop(self) -> None:
        backoff = 5
        while True:
            try:
                before = self.contact_host
                resp = await self.register()
                if resp.status == 200 and self.contact_host != before:
                    resp = await self.register()  # re-register with the NAT-mapped contact
                if resp.status == 200:
                    if not self.registered:
                        log.info("SIP registered as %s at %s (contact %s)", self.config.username,
                                 self.config.server, self.contact())
                    self.registered = True
                    self.last_error = ""
                    backoff = 5
                    m = re.search(r"expires=(\d+)", resp.get("contact"))
                    granted = int(m.group(1)) if m else int(resp.get("expires") or self.config.expires)
                    await asyncio.sleep(max(30, granted * 0.8))
                    continue
                self.registered = False
                self.last_error = f"REGISTER failed: {resp.start}"
                log.warning("SIP %s", self.last_error)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.registered = False
                self.last_error = f"REGISTER error: {exc}"
                log.warning("SIP %s", self.last_error)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(25)
            self.sendto(b"\r\n\r\n")

    # ----------------------------------------------------------- outbound
    async def call(self, number: str, on_ringing: Callable[[], None] | None = None) -> SipCall:
        """INVITE `number` via the trunk. Returns when answered; raises SipCallFailed otherwise."""
        number = re.sub(r"[^\d*#]", "", number)
        call = SipCall(self, secrets.token_hex(12), "outbound", number)
        call.local_uri = self.aor()
        call.remote_uri = f"sip:{number}@{self.config.realm_domain}"
        self.calls[call.call_id] = call
        await call._open_rtp(self.config.codecs[0])
        sdp = build_sdp(self.media_ip, call.rtp_port, list(self.config.codecs), random.randint(1, 10**9))
        auth: list[tuple[str, str]] = []

        def provisional(resp: SipMessage) -> None:
            if resp.status in (180, 183) and call.state == "init":
                call.state = "ringing"
                if on_ringing:
                    on_ringing()

        try:
            for _ in range(3):
                call.cseq += 1
                invite = self.new_request(
                    "INVITE", call.remote_uri, call_id=call.call_id, cseq=call.cseq,
                    from_=f"<{call.local_uri}>;tag={call.local_tag}", to=f"<{call.remote_uri}>",
                    extra=[("contact", self.contact()), ("content-type", "application/sdp"),
                           ("allow", "INVITE, ACK, CANCEL, BYE, OPTIONS, UPDATE")] + auth,
                    body=sdp,
                )
                call.invite = invite
                resp = await self.transaction(invite, timeout=90, provisional=provisional)
                if resp.status in (401, 407):
                    self._ack(invite, resp)
                    header = "www-authenticate" if resp.status == 401 else "proxy-authenticate"
                    name = "authorization" if resp.status == 401 else "proxy-authorization"
                    auth = [(name, digest_authorization(resp.get(header), "INVITE", call.remote_uri,
                                                        self.config.username, self.config.password))]
                    continue
                break
        except Exception as exc:
            call._finish(f"invite error: {exc}")
            raise SipCallFailed(0, str(exc)) from exc
        if resp.status >= 300 or call.state == "ended":
            self._ack(invite, resp)
            call._finish(f"{resp.status} {resp.start.split(' ', 2)[-1]}")
            raise SipCallFailed(resp.status, resp.start)
        call.remote_tag = tag_of(resp.get("to"))
        call.remote_target = uri_of(resp.get("contact")) or call.remote_uri
        call.route_set = list(reversed(resp.get_all("record-route")))
        self._ack(invite, resp, uri=call.remote_target, routes=call.route_set)
        ip, port, answered = parse_sdp(resp.body)
        if call.rtp is not None and answered and answered[0] in CODECS:
            call.rtp.pt = answered[0]
        call.state = "answered"
        assert call.rtp is not None
        call.rtp.start((ip, port))
        if not call.answered.done():
            call.answered.set_result(True)
        return call

    # ------------------------------------------------------------ incoming
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if data.strip() == b"":
            return
        msg = parse_message(data)
        if msg is None:
            return
        if msg.is_request:
            asyncio.ensure_future(self._on_request(msg, addr))
            return
        num, method = msg.cseq
        queue = self._pending.get((msg.get("call-id"), num, method))
        if queue is not None:
            queue.put_nowait(msg)
        elif method == "INVITE" and msg.status == 200:
            call = self.calls.get(msg.get("call-id"))
            if call and call.invite is not None:  # retransmitted 200: ACK again
                self._ack(call.invite, msg, uri=call.remote_target, routes=call.route_set)

    async def _on_request(self, msg: SipMessage, addr: tuple[str, int]) -> None:
        method = msg.method
        call = self.calls.get(msg.get("call-id"))
        if method == "INVITE":
            if call is not None:
                if call.state == "answered" and call._ok is not None and tag_of(msg.get("to")):
                    # re-INVITE (session refresh/hold): keep the same media
                    sdp = build_sdp(self.media_ip, call.rtp_port, [call.rtp.pt if call.rtp else 8], random.randint(1, 10**9))
                    self.send_response(msg, 200, "OK", addr, extra=[("contact", self.contact()),
                                       ("content-type", "application/sdp")], body=sdp)
                elif call._ok is not None:
                    self.sendto(call._ok, addr)
                return
            await self._incoming_invite(msg, addr)
        elif method == "ACK":
            if call is not None:
                call._acked.set()
        elif method == "BYE":
            self.send_response(msg, 200, "OK", addr)
            if call is not None:
                call._finish("remote hangup")
        elif method == "CANCEL":
            self.send_response(msg, 200, "OK", addr)
            if call is not None and call.state != "answered" and call.invite is not None:
                self.send_response(call.invite, 487, "Request Terminated", addr, to_tag=call.local_tag)
                call._finish("caller cancelled")
        elif method in ("OPTIONS", "NOTIFY", "UPDATE", "INFO"):
            self.send_response(msg, 200, "OK", addr, extra=[("allow", "INVITE, ACK, CANCEL, BYE, OPTIONS, UPDATE")])
        else:
            self.send_response(msg, 405, "Method Not Allowed", addr)

    async def _incoming_invite(self, msg: SipMessage, addr: tuple[str, int]) -> None:
        self.send_response(msg, 100, "Trying", addr)
        caller = user_of(msg.get("from"))
        call = SipCall(self, msg.get("call-id"), "inbound", caller)
        call.invite, call.invite_addr = msg, addr
        call.remote_tag = tag_of(msg.get("from"))
        call.remote_uri = uri_of(msg.get("from"))
        call.local_uri = uri_of(msg.get("to"))
        call.remote_target = uri_of(msg.get("contact")) or call.remote_uri
        call.route_set = msg.get_all("record-route")
        call.cseq = random.randint(1, 1000)
        self.calls[call.call_id] = call
        log.info("SIP inbound call from %s", caller or "unknown")
        if self.on_incoming is None:
            call.reject(480, "Temporarily Unavailable")
            return
        try:
            await self.on_incoming(call)
        except Exception:  # noqa: BLE001
            log.exception("inbound call handler failed")
            if call.state not in ("answered", "ended"):
                call.reject(500, "Server Error")


class SipCallFailed(RuntimeError):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
