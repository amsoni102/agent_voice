"""Local speech-to-text (faster-whisper). Loaded once, CPU is fine for tiny/base."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .audio_util import resample_s16, s16_to_float32

log = logging.getLogger("agentvoice.stt")

_model: Any = None
_lock = asyncio.Lock()


async def transcribe_pcm16(pcm: bytes, sample_rate: int, model_size: str = "tiny.en") -> str:
    pcm16k = resample_s16(pcm, sample_rate, 16000)
    samples = s16_to_float32(pcm16k)
    if not samples:
        return ""
    model = await _get_model(model_size)

    def _run() -> str:
        segments, _info = model.transcribe(
            samples,
            language="en",
            beam_size=1,
            vad_filter=False,
            without_timestamps=True,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    text = await asyncio.to_thread(_run)
    log.info("stt: %s", text[:160])
    return text


async def _get_model(model_size: str) -> Any:
    global _model
    async with _lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "faster-whisper is not installed. In the project venv run: pip install faster-whisper"
                ) from exc
            log.info("loading Whisper %s (CPU)", model_size)
            _model = await asyncio.to_thread(
                WhisperModel, model_size, device="cpu", compute_type="int8"
            )
        return _model
