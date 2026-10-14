"""
stt.py

Speech-to-text for the voice pipeline. Takes the raw audio the watch sends
(PCM, 16 kHz, signed 16-bit little-endian, mono, no WAV header - see the
LilygoFirmware BLE protocol) and returns text using faster-whisper on CPU.

Tuned for a small CPU-only server (2 cores / 4 threads, no usable GPU):
  * int8 quantisation, greedy decoding (beam_size=1)
  * the model is loaded lazily on first use and kept in memory
  * only one transcription runs at a time (a lock), so a burst of requests
    cannot starve the machine
  * near-silent clips are rejected before Whisper sees them - Whisper tends
    to hallucinate phrases on silence, and the watch's PDM microphone is noisy

Everything is configurable through environment variables (see Settings).

Requires: pip install faster-whisper numpy
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np

logger = logging.getLogger("stt")

SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2  # signed 16-bit little-endian


class AudioError(ValueError):
    """The audio payload is unusable (wrong size, too short, silent...)."""


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    model: str = field(default_factory=lambda: os.environ.get("STT_MODEL", "small"))
    language: str = field(default_factory=lambda: os.environ.get("STT_LANGUAGE", "ru"))
    compute_type: str = field(default_factory=lambda: os.environ.get("STT_COMPUTE_TYPE", "int8"))
    cpu_threads: int = field(default_factory=lambda: _env_int("STT_CPU_THREADS", 4))
    model_dir: str | None = field(default_factory=lambda: os.environ.get("STT_MODEL_DIR") or None)
    # Hint that biases Whisper towards the vocabulary of server commands.
    initial_prompt: str = field(
        default_factory=lambda: os.environ.get(
            "STT_INITIAL_PROMPT",
            "Команды для сервера: статус, диск, память, перезапусти сервис, "
            "docker, systemd, логи, обнови, остановись.",
        )
    )
    # Clips quieter than this RMS (0..1 scale) are treated as silence.
    min_rms: float = field(default_factory=lambda: _env_float("STT_MIN_RMS", 0.003))
    # Shorter than this is not a command.
    min_seconds: float = field(default_factory=lambda: _env_float("STT_MIN_SECONDS", 0.4))
    # The firmware records at most 8 s; allow a little slack.
    max_seconds: float = field(default_factory=lambda: _env_float("STT_MAX_SECONDS", 10.0))


def pcm16_to_float32(data: bytes) -> np.ndarray:
    """Raw s16le mono bytes -> float32 array in [-1, 1) (what Whisper wants)."""
    if len(data) % BYTES_PER_SAMPLE:
        raise AudioError(
            f"PCM payload has an odd number of bytes ({len(data)}); expected 16-bit samples"
        )
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


class Transcriber:
    """Lazy, thread-safe wrapper around a faster-whisper model.

    `model` can be injected (tests do this) - anything with a
    `transcribe(audio, **kwargs) -> (segments, info)` method works.
    """

    def __init__(self, settings: Settings | None = None, model: Any | None = None) -> None:
        self.settings = settings or Settings()
        self._model = model
        self._load_lock = threading.Lock()
        # Serialises transcriptions: the CPU is the bottleneck, not RAM.
        self._run_lock = threading.Lock()

    # -- model ----------------------------------------------------------

    def _get_model(self) -> Any:
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from faster_whisper import WhisperModel  # heavy import, keep lazy

                    s = self.settings
                    logger.info(
                        "Loading Whisper model=%s compute_type=%s threads=%s",
                        s.model, s.compute_type, s.cpu_threads,
                    )
                    self._model = WhisperModel(
                        s.model,
                        device="cpu",
                        compute_type=s.compute_type,
                        cpu_threads=s.cpu_threads,
                        download_root=s.model_dir,
                    )
        return self._model

    def warm_up(self) -> None:
        """Load the model now (call at startup so the first command is not slow)."""
        self._get_model()

    # -- audio checks ---------------------------------------------------

    def validate(self, pcm: bytes) -> np.ndarray:
        """Check the payload and return float32 samples, or raise AudioError."""
        s = self.settings
        audio = pcm16_to_float32(pcm)
        seconds = len(audio) / SAMPLE_RATE
        if seconds < s.min_seconds:
            raise AudioError(f"Audio too short ({seconds:.2f}s)")
        if seconds > s.max_seconds:
            raise AudioError(f"Audio too long ({seconds:.2f}s, max {s.max_seconds:.0f}s)")
        rms = float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0
        if rms < s.min_rms:
            raise AudioError(f"Audio is silent (rms={rms:.4f})")
        return audio

    # -- transcription --------------------------------------------------

    def transcribe(self, pcm: bytes, language: str | None = None) -> str:
        """Blocking transcription of raw PCM. Returns "" if nothing was recognised.
        language: Whisper language code, "auto" to detect it, None for the
        configured default (STT_LANGUAGE)."""
        audio = self.validate(pcm)
        model = self._get_model()
        s = self.settings
        if language is None:
            language = s.language
        if language == "auto":
            language = None

        with self._run_lock:
            segments, info = model.transcribe(
                audio,
                language=language,
                beam_size=1,                       # greedy: fastest on CPU
                vad_filter=True,                   # drop non-speech before decoding
                vad_parameters={"min_silence_duration_ms": 300},
                condition_on_previous_text=False,  # no cross-segment hallucination loops
                no_speech_threshold=0.6,
                initial_prompt=s.initial_prompt or None,
                temperature=0.0,
            )
            # `segments` is a lazy generator: decoding happens while iterating.
            text = " ".join(seg.text.strip() for seg in segments).strip()

        logger.info(
            "Transcribed %.1fs of audio (lang=%s p=%.2f): %r",
            len(audio) / SAMPLE_RATE,
            getattr(info, "language", "?"),
            getattr(info, "language_probability", 0.0),
            text,
        )
        return text


_default: Transcriber | None = None
_default_lock = threading.Lock()


def get_transcriber() -> Transcriber:
    """Process-wide shared Transcriber (one model in memory)."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = Transcriber()
    return _default
