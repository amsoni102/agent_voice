"""Local speech-to-text (faster-whisper). Loaded once, CPU is fine for tiny/base."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .audio_util import resample_s16

log = logging.getLogger("agentvoice.stt")

_model: Any = None
_model_size = ""
_load_lock = asyncio.Lock()
# One decode at a time: concurrent decodes on the same CPU cores only slow each other down.
_decode_lock = asyncio.Lock()


async def transcribe_pcm16(pcm: bytes, sample_rate: int, model_size: str = "tiny.en") -> str:
    import numpy as np

    pcm16k = resample_s16(pcm, sample_rate, 16000)
    if len(pcm16k) < 3200:  # < 100 ms
        return ""
    audio = np.frombuffer(pcm16k[: len(pcm16k) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
    model = await _get_model(model_size)

    def _run() -> str:
        segments, _info = model.transcribe(
            audio,
            language="en",
            beam_size=1,
            # A single greedy pass: the default temperature fallback re-decodes "uncertain"
            # audio up to 5 times, which is where multi-second stalls come from.
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    async with _decode_lock:
        text = await asyncio.to_thread(_run)
    log.info("stt: %s", text[:160])
    return text


async def preload(model_size: str = "tiny.en") -> None:
    try:
        await _get_model(model_size)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not preload Whisper %s: %s", model_size, exc)


async def _get_model(model_size: str) -> Any:
    global _model, _model_size
    async with _load_lock:
        if _model is None or _model_size != model_size:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "faster-whisper is not installed. In the project venv run: pip install faster-whisper"
                ) from exc
            threads = min(8, os.cpu_count() or 4)
            log.info("loading Whisper %s (CPU, %d threads)", model_size, threads)
            _model = await asyncio.to_thread(
                WhisperModel, model_size, device="cpu", compute_type="int8", cpu_threads=threads
            )
            _model_size = model_size
        return _model
