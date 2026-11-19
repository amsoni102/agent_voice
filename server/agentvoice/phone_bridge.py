"""Connects a voice session (PCM16 24 kHz) to a real-time phone line that moves fixed 20 ms frames."""

from __future__ import annotations

import asyncio
import audioop
import base64
import logging
from typing import Any, Awaitable, Callable

from .realtime import Transport

log = logging.getLogger("agentvoice.phone")

SESSION_RATE = 24000
FRAME_MS = 20


class PhoneBridge(Transport):
    """The session sends/receives 24 kHz PCM16; the line pulls and pushes PCM16 at `line_rate`.

    The line calls `next_frame()` every 20 ms (its own clock paces playback) and `receive()` with
    caller audio. Marks fire once the audio queued before them has actually gone out on the line.
    """

    audio_format = {"type": "audio/pcm", "rate": SESSION_RATE}
    bytes_per_ms = SESSION_RATE * 2 / 1000

    def __init__(self, line_rate: int, on_hangup: Callable[[], Awaitable[None]], label: str = "phone"):
        self.line_rate = line_rate
        self.frame_bytes = line_rate * 2 * FRAME_MS // 1000
        self.label = label
        self.session: Any = None
        self._on_hangup = on_hangup
        self._out = bytearray()
        self._queued = 0  # line-rate bytes ever queued
        self._played = 0  # line-rate bytes ever sent out
        self._marks: list[tuple[int, str]] = []
        self._rs_out: Any = None
        self._rs_in: Any = None
        self._hung_up = False

    # ------------------------------------------------------------ session side
    async def send_audio(self, b64_audio: str) -> None:
        pcm = base64.b64decode(b64_audio)
        if self.line_rate != SESSION_RATE:
            pcm, self._rs_out = audioop.ratecv(pcm, 2, 1, SESSION_RATE, self.line_rate, self._rs_out)
        self._out.extend(pcm)
        self._queued += len(pcm)

    async def clear(self) -> None:
        self._out.clear()
        self._played = self._queued
        self._rs_out = None
        self._fire_marks()

    async def mark(self, name: str) -> None:
        self._marks.append((self._queued, name))
        if self._queued <= self._played:
            self._fire_marks()

    async def hangup(self) -> None:
        if self._hung_up:
            return
        self._hung_up = True
        await self._on_hangup()

    async def send_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "transcript":
            log.info("%s %s: %s", self.label, event.get("role"), event.get("text"))

    # --------------------------------------------------------------- line side
    def next_frame(self) -> bytes:
        """One 20 ms frame of agent audio at line rate (silence when the agent is quiet)."""
        n = self.frame_bytes
        if self._out:
            frame = bytes(self._out[:n])
            del self._out[:n]
            self._played += len(frame)
            if len(frame) < n:
                frame += b"\x00" * (n - len(frame))
            if self._marks:
                self._fire_marks()
            return frame
        return b"\x00" * n

    async def receive(self, pcm: bytes) -> None:
        """Caller audio (PCM16 at line rate) into the voice session."""
        if self.session is None or not pcm:
            return
        if self.line_rate != SESSION_RATE:
            pcm, self._rs_in = audioop.ratecv(pcm, 2, 1, self.line_rate, SESSION_RATE, self._rs_in)
        await self.session.feed_audio(base64.b64encode(pcm).decode())

    @property
    def playing(self) -> bool:
        return bool(self._out)

    def _fire_marks(self) -> None:
        due = [name for offset, name in self._marks if offset <= self._played]
        if not due:
            return
        self._marks = [(o, n) for o, n in self._marks if o > self._played]
        session = self.session
        if session is None:
            return
        for name in due:
            asyncio.ensure_future(session.on_mark(name))
