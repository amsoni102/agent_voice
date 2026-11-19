"""PCM helpers for the local (Ollama) voice path."""

from __future__ import annotations

import array
import audioop


def resample_s16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not data:
        return data
    converted, _ = audioop.ratecv(data, 2, 1, src_rate, dst_rate, None)
    return converted


def rms_s16(data: bytes) -> float:
    if len(data) < 2:
        return 0.0
    return float(audioop.rms(data, 2))


def s16_to_float32(data: bytes) -> list[float]:
    samples = array.array("h")
    samples.frombytes(data[: len(data) - (len(data) % 2)])
    return [s / 32768.0 for s in samples]


class EnergyVad:
    """Simple energy VAD. Good enough for close-mic practice on the Android app."""

    def __init__(
        self,
        sample_rate: int = 24000,
        frame_ms: int = 20,
        start_rms: float = 450,
        end_rms: float = 280,
        start_frames: int = 4,
        end_ms: int = 750,
        min_speech_ms: int = 280,
        max_speech_ms: int = 15000,
    ):
        self.frame_bytes = int(sample_rate * 2 * frame_ms / 1000)
        self.start_rms = start_rms
        self.end_rms = end_rms
        self.start_frames = start_frames
        self.end_frames = max(1, end_ms // frame_ms)
        self.min_speech_bytes = int(sample_rate * 2 * min_speech_ms / 1000)
        self.max_speech_bytes = int(sample_rate * 2 * max_speech_ms / 1000)
        self._buf = bytearray()
        self._speech = bytearray()
        self._in_speech = False
        self._loud = 0
        self._quiet = 0

    def reset(self) -> None:
        self._buf.clear()
        self._speech.clear()
        self._in_speech = False
        self._loud = 0
        self._quiet = 0

    def push(self, pcm: bytes) -> bytes | None:
        """Feed PCM16 mono. Returns a complete utterance, or None."""
        self._buf.extend(pcm)
        utterance: bytes | None = None
        while len(self._buf) >= self.frame_bytes:
            frame = bytes(self._buf[: self.frame_bytes])
            del self._buf[: self.frame_bytes]
            utterance = self._frame(frame) or utterance
        return utterance

    def _frame(self, frame: bytes) -> bytes | None:
        energy = rms_s16(frame)
        if not self._in_speech:
            if energy >= self.start_rms:
                self._loud += 1
                self._speech.extend(frame)
                if self._loud >= self.start_frames:
                    self._in_speech = True
                    self._quiet = 0
            else:
                self._loud = 0
                self._speech.clear()
            return None
        self._speech.extend(frame)
        if energy < self.end_rms:
            self._quiet += 1
        else:
            self._quiet = 0
        too_long = len(self._speech) >= self.max_speech_bytes
        ended = self._quiet >= self.end_frames or too_long
        if not ended:
            return None
        speech = bytes(self._speech)
        self._speech.clear()
        self._in_speech = False
        self._loud = 0
        self._quiet = 0
        if len(speech) < self.min_speech_bytes:
            return None
        return speech


def split_sentences(text: str) -> list[str]:
    buf = []
    out = []
    for ch in text:
        buf.append(ch)
        if ch in ".!?:" and len(buf) > 8:
            piece = "".join(buf).strip()
            if piece:
                out.append(piece)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out or ([text.strip()] if text.strip() else [])


def pcm16_chunk_b64(data: bytes, chunk_bytes: int = 4800) -> list[bytes]:
    """Yield PCM chunks sized for ~100ms at 24 kHz (4800 bytes)."""
    return [data[i : i + chunk_bytes] for i in range(0, len(data), chunk_bytes) if data[i : i + chunk_bytes]]
