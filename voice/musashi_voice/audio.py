"""Microphone capture (sounddevice) + end trimming (Silero VAD).

Activation in V1 is **push-to-talk**, not a wake word — the plan's reasoning:
PTT is more reliable than wake word with background conversation, it satisfies
Plano Diretor §2.7's demand for a second non-audio factor, and it neutralises
the DolphinAttack / speaker-replay class by construction. `record_utterance()`
therefore records for exactly as long as an externally-owned event says to;
this phase's harness is a terminal key (see __main__), and the real guest PTT
gesture is a later integration that changes nothing in this file.

Silero VAD is *not* deciding the end of turn here — the human already did.
It only trims silence off the ends, which is worth doing anyway: it is what
kills Whisper's hallucination on mute audio and shortens the inference.

Both sounddevice and silero-vad are imported lazily. This module must remain
importable (and the rest of the package testable) on a machine with no audio
device and no torch.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger("musashi-voice.audio")

SAMPLERATE = 16000       # Silero and Whisper both want exactly this


class AudioUnavailable(RuntimeError):
    """No usable capture device / sounddevice not installed."""


def _sd():
    try:
        import sounddevice as sd
    except Exception as exc:                              # noqa: BLE001
        raise AudioUnavailable(f"sounddevice unavailable: {exc}") from exc
    return sd


def list_devices() -> str:
    return str(_sd().query_devices())


def record_utterance(stop: threading.Event, cfg: dict | None = None,
                     on_start=None) -> np.ndarray:
    """Record mono float32 PCM while `stop` is unset. Returns the raw audio.

    `stop` is the push-to-talk edge, owned by whoever is driving: the terminal
    harness sets it on the second Enter; the guest's PTT gesture will set it
    when the hand opens. `max_seconds` is a safety net so a stuck PTT cannot
    record until the machine runs out of memory.
    """
    cfg = cfg or {}
    sd = _sd()
    samplerate = int(cfg.get("samplerate", SAMPLERATE))
    blocksize = int(cfg.get("blocksize", 512))
    max_frames = int(float(cfg.get("max_seconds", 30.0)) * samplerate)
    device = cfg.get("input_device") or None

    chunks: list[np.ndarray] = []
    frames = 0
    stream = sd.InputStream(samplerate=samplerate, channels=int(cfg.get("channels", 1)),
                            dtype="float32", blocksize=blocksize, device=device)
    with stream:
        if on_start is not None:
            on_start()
        while not stop.is_set() and frames < max_frames:
            block, overflowed = stream.read(blocksize)
            if overflowed:
                log.warning("input overflow: dropped audio")
            chunks.append(block.copy())
            frames += len(block)
    if frames >= max_frames:
        log.warning("hit max_seconds; stopped recording")

    if not chunks:
        return np.zeros(0, dtype=np.float32)
    pcm = np.concatenate(chunks, axis=0).reshape(-1).astype(np.float32)
    log.info("recorded %.2f s (%d samples)", len(pcm) / samplerate, len(pcm))
    return pcm


# -- VAD -----------------------------------------------------------------
_vad_model = None


def _load_vad():
    """pip `silero-vad` first — it is the lowest-friction install and pins its
    own bundled ONNX/JIT weights, so there is no torch.hub cache to warm and
    no network at first use. Returns None if it is not installed."""
    global _vad_model
    if _vad_model is not None:
        return _vad_model
    try:
        from silero_vad import load_silero_vad
        _vad_model = load_silero_vad()
    except Exception as exc:                              # noqa: BLE001
        log.warning("Silero VAD unavailable (%s); passing audio through untrimmed", exc)
        return None
    return _vad_model


def trim_silence(pcm: np.ndarray, cfg: dict | None = None,
                 samplerate: int = SAMPLERATE) -> np.ndarray:
    """Cut leading/trailing silence. Returns `pcm` unchanged if VAD is absent
    or finds no speech — never returns empty audio for non-empty input, since
    "the VAD found nothing" and "the user said nothing" are the STT's problem
    to report, not ours to guess at."""
    cfg = cfg or {}
    if not cfg.get("enabled", True) or pcm.size == 0:
        return pcm
    model = _load_vad()
    if model is None:
        return pcm

    try:
        import torch
        from silero_vad import get_speech_timestamps
        spans = get_speech_timestamps(
            torch.from_numpy(pcm), model,
            sampling_rate=samplerate,
            threshold=float(cfg.get("threshold", 0.5)),
        )
    except Exception as exc:                              # noqa: BLE001
        log.warning("VAD failed (%s); passing audio through untrimmed", exc)
        return pcm

    if not spans:
        log.info("VAD found no speech in %.2f s", len(pcm) / samplerate)
        return pcm

    pad = int(float(cfg.get("pad_ms", 150)) / 1000.0 * samplerate)
    start = max(0, spans[0]["start"] - pad)
    end = min(len(pcm), spans[-1]["end"] + pad)
    log.info("VAD trimmed %.2f s -> %.2f s", len(pcm) / samplerate, (end - start) / samplerate)
    return pcm[start:end]
