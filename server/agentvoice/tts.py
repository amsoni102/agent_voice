"""Local TTS: Kokoro (natural neural voice), then Piper, then espeak-ng as last resort."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import struct
import threading
import wave
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from .audio_util import resample_s16

log = logging.getLogger("agentvoice.tts")

DEFAULT_PIPER_DIR = Path.home() / ".local/share/management-assistant/voice"
DEFAULT_PIPER_BIN = DEFAULT_PIPER_DIR / "piper" / "piper"
DEFAULT_PIPER_MODEL = DEFAULT_PIPER_DIR / "en_US-ryan-high.onnx"
DEFAULT_KOKORO_DIR = Path.home() / ".local/share/agentvoice/kokoro"
PIPER_RATE = 22050
OUT_RATE = 24000


class KokoroVoice:
    """Kokoro-82M via onnxruntime; output is already 24 kHz."""

    def __init__(self, model: str, voices: str, voice: str = "af_heart", speed: float = 1.0):
        self.model, self.voices, self.voice, self.speed = model, voices, voice, speed
        self._lock = threading.Lock()

    def available(self) -> bool:
        if not (Path(self.model).is_file() and Path(self.voices).is_file()):
            return False
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return False
        return True

    def synth(self, text: str) -> bytes:
        import numpy as np

        engine = _kokoro_engine(self.model, self.voices)
        # One synthesis at a time: parallel runs just fight over the same CPU cores.
        with self._lock:
            samples, rate = engine.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        return pcm if rate == OUT_RATE else resample_s16(pcm, rate, OUT_RATE)

    async def preload(self) -> None:
        if self.available():
            await asyncio.to_thread(self.synth, "Hi.")


@lru_cache(maxsize=2)
def _kokoro_engine(model: str, voices: str):  # noqa: ANN202
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    opts = ort.SessionOptions()
    # Physical cores only: hyperthreads make the convolutions slower, not faster.
    opts.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(model, opts, providers=["CPUExecutionProvider"])
    return Kokoro.from_session(session, voices)


async def speak_pcm24k(
    text: str, piper_bin: str, piper_model: str, *, kokoro: KokoroVoice | None = None
) -> bytes:
    text = " ".join((text or "").split())
    if not text:
        return b""
    if kokoro is not None and kokoro.available():
        try:
            return await asyncio.to_thread(kokoro.synth, text)
        except Exception:  # noqa: BLE001
            log.exception("kokoro failed; falling back to Piper")
    bin_path = Path(piper_bin) if piper_bin else DEFAULT_PIPER_BIN
    model_path = Path(piper_model) if piper_model else DEFAULT_PIPER_MODEL
    if bin_path.is_file() and model_path.is_file():
        raw = await _piper(text, bin_path, model_path)
        if raw:
            return resample_s16(raw, PIPER_RATE, OUT_RATE)
    raw = await _espeak(text)
    return resample_s16(raw, 22050, OUT_RATE) if raw else b""


async def _piper(text: str, binary: Path, model: Path) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        str(binary),
        "--model",
        str(model),
        "--output_raw",
        "--length_scale",
        "1.0",
        "--sentence_silence",
        "0.25",
        "--quiet",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(binary.parent),
    )
    stdout, _ = await proc.communicate(text.encode("utf-8"))
    if proc.returncode != 0:
        log.warning("piper exited %s", proc.returncode)
        return b""
    return stdout or b""


async def _espeak(text: str) -> bytes:
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        log.warning("no Piper voice and no espeak-ng — cannot speak")
        return b""
    proc = await asyncio.create_subprocess_exec(
        exe,
        "-v",
        "en-us",
        "-s",
        "160",
        "--stdout",
        text,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return _wav_pcm(stdout) if stdout else b""


def _wav_pcm(blob: bytes) -> bytes:
    if blob[:4] != b"RIFF":
        return blob
    try:
        with wave.open(BytesIO(blob), "rb") as wav:
            raw = wav.readframes(wav.getnframes())
            if wav.getnchannels() == 2:
                raw = b"".join(raw[i : i + 2] for i in range(0, len(raw), 4))
            if wav.getsampwidth() != 2:
                return b""
            return resample_s16(raw, wav.getframerate(), PIPER_RATE)
    except (wave.Error, struct.error):
        return b""
