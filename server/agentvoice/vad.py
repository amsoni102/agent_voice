"""Streaming end-of-turn detection with the Silero VAD model bundled in faster-whisper."""

from __future__ import annotations

import audioop
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("agentvoice.vad")

VAD_RATE = 16000
FRAME = 512  # samples at 16 kHz = 32 ms, the model's native window
CONTEXT = 64
FRAME_MS = FRAME * 1000 // VAD_RATE

_session: Any = None


def _load_session() -> Any:
    global _session
    if _session is None:
        import faster_whisper
        import onnxruntime

        path = Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx"
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        _session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=opts)
    return _session


def silero_available() -> bool:
    try:
        _load_session()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Silero VAD unavailable, falling back to energy VAD: %s", exc)
        return False


@dataclass
class VadEvent:
    kind: str  # "start" | "pause" | "resume" | "end"
    audio: bytes = b""  # PCM16 mono at 16 kHz (pause/end): the utterance so far


class SileroEndpointer:
    """Feeds 24 kHz PCM16, emits turn events.

    "pause" fires after a short silence so speech recognition can start early;
    "end" confirms the turn after a longer one; "resume" means the pause was mid-sentence.
    Hysteresis (separate start/stop probabilities) keeps breaths and noise from flapping it.
    """

    def __init__(
        self,
        in_rate: int = 24000,
        start_prob: float = 0.5,
        stop_prob: float = 0.35,
        start_ms: int = 64,
        pause_ms: int = 180,
        end_ms: int = 450,
        preroll_ms: int = 320,
        min_speech_ms: int = 160,
        max_speech_ms: int = 15000,
    ):
        import numpy as np

        self._np = np
        self.session = _load_session()
        self.in_rate = in_rate
        self.start_prob, self.stop_prob = start_prob, stop_prob
        self.start_frames = max(1, start_ms // FRAME_MS)
        self.pause_frames = max(1, pause_ms // FRAME_MS)
        self.end_frames = max(self.pause_frames + 1, end_ms // FRAME_MS)
        self.min_speech_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_bytes = VAD_RATE * 2 * max_speech_ms // 1000
        self._preroll: deque[bytes] = deque(maxlen=max(1, preroll_ms // FRAME_MS))
        self._rs_state: Any = None
        self.reset()

    def reset(self) -> None:
        np = self._np
        self._h = np.zeros((1, 1, 128), np.float32)
        self._c = np.zeros((1, 1, 128), np.float32)
        self._ctx = np.zeros(CONTEXT, np.float32)
        self._pending = bytearray()
        self._speech = bytearray()
        self._preroll.clear()
        self._in_speech = False
        self._loud = 0
        self._quiet = 0
        self._voiced = 0
        self._paused = False
        self.last_prob = 0.0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def voiced_ms(self) -> int:
        return self._voiced * FRAME_MS if self._in_speech else 0

    def speech_so_far(self) -> bytes:
        return bytes(self._speech)

    def push(self, pcm: bytes) -> list[VadEvent]:
        if self.in_rate != VAD_RATE:
            pcm, self._rs_state = audioop.ratecv(pcm, 2, 1, self.in_rate, VAD_RATE, self._rs_state)
        self._pending.extend(pcm)
        events: list[VadEvent] = []
        step = FRAME * 2
        while len(self._pending) >= step:
            frame = bytes(self._pending[:step])
            del self._pending[:step]
            ev = self._frame(frame)
            if ev is not None:
                events.append(ev)
        return events

    def _prob(self, frame: bytes) -> float:
        np = self._np
        x = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        inp = np.concatenate([self._ctx, x])[None, :]
        self._ctx = x[-CONTEXT:]
        out, self._h, self._c = self.session.run(None, {"input": inp, "h": self._h, "c": self._c})
        return float(out.ravel()[0])

    def _frame(self, frame: bytes) -> VadEvent | None:
        p = self.last_prob = self._prob(frame)
        if not self._in_speech:
            self._preroll.append(frame)
            self._loud = self._loud + 1 if p >= self.start_prob else 0
            if self._loud >= self.start_frames:
                self._in_speech = True
                self._speech = bytearray(b"".join(self._preroll))
                self._preroll.clear()
                self._quiet = 0
                self._voiced = self._loud
                self._paused = False
                return VadEvent("start")
            return None

        self._speech.extend(frame)
        if p >= self.stop_prob:
            self._voiced += 1
            self._quiet = 0
            if self._paused:
                self._paused = False
                return VadEvent("resume")
            if len(self._speech) < self.max_bytes:
                return None
        else:
            self._quiet += 1

        too_long = len(self._speech) >= self.max_bytes
        if self._quiet >= self.end_frames or too_long:
            speech = bytes(self._speech)
            enough = self._voiced >= self.min_speech_frames
            self._in_speech = False
            self._speech = bytearray()
            self._loud = self._quiet = self._voiced = 0
            self._paused = False
            return VadEvent("end", speech) if enough else VadEvent("end")
        if self._quiet == self.pause_frames and self._voiced >= self.min_speech_frames:
            self._paused = True
            return VadEvent("pause", bytes(self._speech))
        return None
