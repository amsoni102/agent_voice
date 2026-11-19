from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from .realtime import Transport
from .twilio import TwilioClient, TwilioError, transfer_twiml

log = logging.getLogger("agentvoice.transport")


async def _safe_send(ws: WebSocket, payload: dict[str, Any]) -> None:
    if ws.application_state != WebSocketState.CONNECTED or ws.client_state != WebSocketState.CONNECTED:
        return
    try:
        await ws.send_json(payload)
    except (RuntimeError, OSError):
        pass


class TwilioTransport(Transport):
    """Twilio bidirectional Media Stream: G.711 u-law 8 kHz, passed through without transcoding."""

    audio_format = {"type": "audio/pcmu"}
    bytes_per_ms = 8.0

    def __init__(self, ws: WebSocket, twilio: TwilioClient | None, caller_id: str):
        self.ws = ws
        self.twilio = twilio
        self.caller_id = caller_id
        self.stream_sid: str | None = None
        self.call_sid: str | None = None

    async def send_audio(self, b64_audio: str) -> None:
        await _safe_send(self.ws, {"event": "media", "streamSid": self.stream_sid, "media": {"payload": b64_audio}})

    async def clear(self) -> None:
        await _safe_send(self.ws, {"event": "clear", "streamSid": self.stream_sid})

    async def mark(self, name: str) -> None:
        await _safe_send(self.ws, {"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})

    async def hangup(self) -> None:
        if self.twilio and self.call_sid:
            await self.twilio.hangup(self.call_sid)
        if self.ws.application_state == WebSocketState.CONNECTED:
            try:
                await self.ws.close()
            except RuntimeError:
                pass

    async def transfer(self, number: str) -> bool:
        if not (self.twilio and self.call_sid):
            return False
        try:
            await self.twilio.update_twiml(self.call_sid, transfer_twiml(number, self.caller_id))
            return True
        except TwilioError as exc:
            log.warning("transfer failed: %s", exc)
            return False


class BrowserTransport(Transport):
    """Practice mode: the dashboard streams PCM16 24 kHz mic audio and plays the agent's replies."""

    audio_format = {"type": "audio/pcm", "rate": 24000}
    bytes_per_ms = 48.0

    def __init__(self, ws: WebSocket):
        self.ws = ws

    async def send_audio(self, b64_audio: str) -> None:
        await _safe_send(self.ws, {"type": "audio", "audio": b64_audio})

    async def clear(self) -> None:
        await _safe_send(self.ws, {"type": "clear"})

    async def mark(self, name: str) -> None:
        await _safe_send(self.ws, {"type": "mark", "name": name})

    async def hangup(self) -> None:
        await _safe_send(self.ws, {"type": "hangup"})
        if self.ws.application_state == WebSocketState.CONNECTED:
            try:
                await self.ws.close()
            except RuntimeError:
                pass

    async def send_event(self, event: dict[str, Any]) -> None:
        await _safe_send(self.ws, {"type": "event", "event": event})
