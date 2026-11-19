"""Local TTS: Piper (same voice as the management assistant) with espeak-ng fallback."""

from __future__ import annotations

import asyncio
import logging
import shutil
import struct
import wave
from io import BytesIO
from pathlib import Path

from .audio_util import resample_s16

log = logging.getLogger("agentvoice.tts")

DEFAULT_PIPER_DIR = Path.home() / ".local/share/management-assistant/voice"
DEFAULT_PIPER_BIN = DEFAULT_PIPER_DIR / "piper" / "piper"
DEFAULT_PIPER_MODEL = DEFAULT_PIPER_DIR / "en_US-ryan-high.onnx"
PIPER_RATE = 22050
OUT_RATE = 24000


async def speak_pcm24k(text: str, piper_bin: str, piper_model: str) -> bytes:
    text = " ".join((text or "").split())
    if not text:
        return b""
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
