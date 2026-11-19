"""WhatsApp Business Calling: the agent answers / places WhatsApp voice calls on the business number.

Signalling is Cloud API webhooks (field "calls") plus POST /<PHONE_NUMBER_ID>/calls; media is WebRTC.
"""

from __future__ import annotations

import asyncio
import fractions
import hashlib
import hmac
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from .db import now_iso
from .phone_bridge import FRAME_MS, PhoneBridge

try:
    import av
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
except ImportError:  # WhatsApp media needs the "phone" extra; webhook helpers still work.
    av = None  # type: ignore[assignment]
    MediaStreamTrack = object  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from .calls import CallManager

log = logging.getLogger("agentvoice.whatsapp")

WEBRTC_RATE = 24000
_FRAME_SAMPLES = WEBRTC_RATE * FRAME_MS // 1000


class WhatsAppError(RuntimeError):
    pass


def verify_signature(app_secret: str, body: bytes, header: str) -> bool:
    if not app_secret:
        return True
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def call_events(payload: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(event, change value) pairs for every call connect/terminate and call status in a webhook."""
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "calls":
                continue
            value = change.get("value") or {}
            for event in value.get("calls") or []:
                out.append((event, value))
            for status in value.get("statuses") or []:
                if status.get("type", "call") == "call":
                    out.append(({"event": "status", **status}, value))
    return out


def _e164(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    return f"+{digits}" if digits else number


class AgentAudioTrack(MediaStreamTrack):
    """Plays whatever the agent says (silence otherwise), paced at 20 ms."""

    kind = "audio"

    def __init__(self, bridge: PhoneBridge) -> None:
        super().__init__()
        self.bridge = bridge
        self._start: float | None = None
        self._pts = 0

    async def recv(self) -> Any:
        if self._start is None:
            self._start = time.monotonic()
        else:
            self._pts += _FRAME_SAMPLES
            wait = self._start + self._pts / WEBRTC_RATE - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        frame = av.AudioFrame(format="s16", layout="mono", samples=_FRAME_SAMPLES)
        frame.planes[0].update(self.bridge.next_frame())
        frame.sample_rate = WEBRTC_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, WEBRTC_RATE)
        return frame


class WhatsAppCall:
    def __init__(self, line: "WhatsAppLine", wa_call_id: str, direction: str, phone: str):
        self.line = line
        self.wa_call_id = wa_call_id
        self.direction = direction
        self.phone = phone
        self.pc = RTCPeerConnection()
        self.bridge: PhoneBridge | None = None
        self.call_row: dict[str, Any] | None = None
        self.session: Any = None
        self.ended = asyncio.get_event_loop().create_future()
        self._tasks: list[asyncio.Task[Any]] = []

        @self.pc.on("track")
        def on_track(track: Any) -> None:
            if track.kind == "audio":
                self._tasks.append(asyncio.create_task(self._pump_in(track)))

        @self.pc.on("connectionstatechange")
        async def on_state() -> None:
            log.info("whatsapp call %s: media %s", self.wa_call_id[-10:], self.pc.connectionState)
            if self.pc.connectionState in ("failed", "closed"):
                self.finish(f"media {self.pc.connectionState}")

    def attach(self, bridge: PhoneBridge) -> None:
        self.bridge = bridge
        self.pc.addTrack(AgentAudioTrack(bridge))

    async def _pump_in(self, track: Any) -> None:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=WEBRTC_RATE)
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            if self.bridge is None:
                continue
            for out in resampler.resample(frame):
                await self.bridge.receive(bytes(out.planes[0])[: out.samples * 2])

    def finish(self, reason: str) -> None:
        if not self.ended.done():
            self.ended.set_result(reason)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.pc.close()


class WhatsAppLine:
    def __init__(self, manager: "CallManager"):
        self.manager = manager
        s = manager.settings
        self.base = f"https://graph.facebook.com/{s.whatsapp_graph_version}/{s.whatsapp_phone_number_id}/calls"
        self.calls: dict[str, WhatsAppCall] = {}
        self.by_call_id: dict[int, WhatsAppCall] = {}
        self._http = httpx.AsyncClient(timeout=15, headers={"Authorization": f"Bearer {s.whatsapp_token}"})

    async def stop(self) -> None:
        for wa in list(self.calls.values()):
            await self.terminate(wa)
        await self._http.aclose()

    async def api(self, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post(self.base, json={"messaging_product": "whatsapp", **body})
        data: dict[str, Any] = {}
        try:
            data = resp.json()
        except ValueError:
            pass
        if resp.status_code >= 400:
            err = data.get("error") or {}
            raise WhatsAppError(f"{err.get('code', resp.status_code)}: {err.get('message', resp.text[:200])}")
        return data

    # ------------------------------------------------------------- webhook
    async def handle_webhook(self, payload: dict[str, Any]) -> None:
        for event, value in call_events(payload):
            try:
                await self._on_event(event, value)
            except Exception:  # noqa: BLE001
                log.exception("whatsapp event %s failed", event.get("event"))

    async def _on_event(self, event: dict[str, Any], value: dict[str, Any]) -> None:
        kind = event.get("event")
        wa_id = event.get("id", "")
        wa = self.calls.get(wa_id)
        if kind == "connect":
            session = event.get("session") or {}
            if session.get("sdp_type") == "offer" and wa is None:
                await self._inbound(event, value)
            elif session.get("sdp_type") == "answer" and wa is not None:
                await wa.pc.setRemoteDescription(RTCSessionDescription(sdp=session["sdp"], type="answer"))
        elif kind == "terminate":
            if wa is not None:
                wa.finish(f"terminated ({event.get('status', '').lower() or 'ended'})")
        elif kind == "status" and wa is not None:
            status = str(event.get("status", "")).upper()
            if status == "RINGING" and wa.call_row:
                self.manager.db.update_call(wa.call_row["id"], status="ringing")
            elif status == "ACCEPTED" and wa.call_row and wa.session is None:
                await self._go_live(wa, "outbound")
            elif status == "REJECTED":
                if wa.call_row:
                    self.manager.db.update_call(wa.call_row["id"], status="busy", outcome="busy", ended_at=now_iso())
                wa.finish("rejected")

    # ------------------------------------------------------------- inbound
    async def _inbound(self, event: dict[str, Any], value: dict[str, Any]) -> None:
        manager = self.manager
        wa_id = event["id"]
        phone = _e164(event.get("from", "")) if event.get("from") else "whatsapp"
        if not manager.settings.voice_available or not manager.settings.whatsapp_auto_answer:
            await self.api({"call_id": wa_id, "action": "reject"})
            return
        wa = WhatsAppCall(self, wa_id, "inbound", phone)
        self.calls[wa_id] = wa
        lead = manager.db.find_lead_by_phone(phone)
        wa.call_row = manager.db.create_call(
            phone=phone, lead_id=lead["id"] if lead else None,
            campaign_id=lead.get("campaign_id") if lead else None, direction="inbound", kind="whatsapp",
        )
        wa.attach(self._bridge(wa))
        await wa.pc.setRemoteDescription(RTCSessionDescription(sdp=event["session"]["sdp"], type="offer"))
        await wa.pc.setLocalDescription(await wa.pc.createAnswer())
        answer = {"sdp_type": "answer", "sdp": wa.pc.localDescription.sdp}
        await self.api({"call_id": wa_id, "action": "pre_accept", "session": answer})
        await self.api({"call_id": wa_id, "action": "accept", "session": answer})
        log.info("whatsapp call from %s accepted (call %s)", phone, wa.call_row["id"])
        await self._go_live(wa, "inbound")

    # ------------------------------------------------------------ outbound
    async def dial(self, call_row: dict[str, Any], phone: str) -> dict[str, Any]:
        wa = WhatsAppCall(self, "", "outbound", phone)
        wa.call_row = call_row
        wa.attach(self._bridge(wa))
        await wa.pc.setLocalDescription(await wa.pc.createOffer())
        try:
            data = await self.api({
                "to": "".join(ch for ch in phone if ch.isdigit()),
                "action": "connect",
                "session": {"sdp_type": "offer", "sdp": wa.pc.localDescription.sdp},
                "biz_opaque_callback_data": str(call_row["id"]),
            })
        except (WhatsAppError, httpx.HTTPError) as exc:
            await wa.close()
            raise WhatsAppError(str(exc)) from exc
        wa.wa_call_id = (data.get("calls") or [{}])[0].get("id", "")
        self.calls[wa.wa_call_id] = wa
        self.by_call_id[call_row["id"]] = wa
        self.manager.spawn(self._watch_unanswered(wa))
        return self.manager.db.update_call(
            call_row["id"], twilio_sid=f"wa:{wa.wa_call_id}", status="initiated"
        ) or call_row

    async def _watch_unanswered(self, wa: WhatsAppCall) -> None:
        try:
            reason = await asyncio.wait_for(asyncio.shield(wa.ended), 75)
        except asyncio.TimeoutError:
            reason = None
        if wa.session is None:
            if wa.call_row:
                row = self.manager.db.get_call(wa.call_row["id"]) or {}
                if row.get("status") not in ("busy", "failed", "completed"):
                    self.manager.db.update_call(wa.call_row["id"], status="no-answer", outcome="no_answer", ended_at=now_iso())
                if row.get("lead_id"):
                    self.manager._finalize_lead(self.manager.db.get_call(wa.call_row["id"]) or row)
            if reason is None:
                await self.terminate(wa)
            else:
                await self._cleanup(wa)

    # -------------------------------------------------------------- shared
    def _bridge(self, wa: WhatsAppCall) -> PhoneBridge:
        async def hang() -> None:
            await self.terminate(wa)

        return PhoneBridge(WEBRTC_RATE, hang, label=f"whatsapp call {wa.call_row['id'] if wa.call_row else ''}")

    async def _go_live(self, wa: WhatsAppCall, direction: str) -> None:
        assert wa.call_row is not None and wa.bridge is not None
        db = self.manager.db
        wa.call_row = db.update_call(
            wa.call_row["id"], twilio_sid=f"wa:{wa.wa_call_id}", status="in-progress", answered_at=now_iso()
        ) or wa.call_row
        self.by_call_id[wa.call_row["id"]] = wa
        wa.session = self.manager.start_line_session(wa.call_row, wa.bridge, direction)
        self.manager.spawn(self._run(wa))

    async def _run(self, wa: WhatsAppCall) -> None:
        assert wa.call_row is not None
        starter = asyncio.create_task(wa.session.start())
        try:
            reason = await wa.ended
            log.info("whatsapp call %s ended: %s", wa.call_row["id"], reason)
        finally:
            starter.cancel()
            row = self.manager.db.get_call(wa.call_row["id"]) or {}
            if row.get("status") not in ("completed", "failed", "busy", "no-answer"):
                self.manager.db.update_call(wa.call_row["id"], status="completed", ended_at=row.get("ended_at") or now_iso())
            await wa.session.stop("whatsapp call ended")
            if row.get("lead_id"):
                self.manager._finalize_lead(self.manager.db.get_call(wa.call_row["id"]) or row)
            await self._cleanup(wa)

    async def terminate(self, wa: WhatsAppCall) -> None:
        if wa.wa_call_id and not wa.ended.done():
            try:
                await self.api({"call_id": wa.wa_call_id, "action": "terminate"})
            except (WhatsAppError, httpx.HTTPError) as exc:
                log.warning("whatsapp terminate failed: %s", exc)
        wa.finish("agent hung up")
        if wa.session is None:
            await self._cleanup(wa)

    async def _cleanup(self, wa: WhatsAppCall) -> None:
        self.calls.pop(wa.wa_call_id, None)
        if wa.call_row:
            self.by_call_id.pop(wa.call_row["id"], None)
        await wa.close()

    async def hangup(self, call_id: int) -> bool:
        wa = self.by_call_id.get(call_id)
        if wa is None:
            return False
        await self.terminate(wa)
        return True
