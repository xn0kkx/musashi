"""Speech to text: faster-whisper, GPU-first with an honest CPU fallback.

Model choice comes from the plan's survey: faster-whisper `large-v3-turbo` in
`int8_float16` on GPU. Moonshine is EN/ZH only, Vosk pt-0.3 scores 68.9% on
CORAA, and Parakeet TDT is the *production* target (Plano Diretor §2.6) whose
NeMo setup friction is not worth paying before the 900 ms budget is measured.

Two adaptive decisions, both because the host this runs on is not guaranteed:

  device       "auto" asks torch whether CUDA is really usable and, if torch
               is not installed at all, still *tries* cuda and catches the
               instantiation failure. CTranslate2 raises at construction time,
               not at first transcribe, so the fallback is safe and cheap.
  compute_type "auto" -> int8_float16 on cuda, int8 on cpu.

`vad_filter=True` is the plan's anti-hallucination setting and is separate
from audio.trim_silence: that one shortens what we send, this one stops
Whisper from inventing words inside internal pauses.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("musashi-voice.stt")


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    duration_s: float = 0.0


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:                                     # noqa: BLE001
        # No torch is not the same as no CUDA: faster-whisper ships its own
        # CTranslate2 runtime. Let the model constructor be the judge.
        return True


class Transcriber:
    """Lazily-loaded faster-whisper model. Load once, transcribe many."""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.model_name = str(cfg.get("model", "large-v3-turbo"))
        self.device_pref = str(cfg.get("device", "auto"))
        self.compute_pref = str(cfg.get("compute_type", "auto"))
        lang = str(cfg.get("language", "pt"))
        self.language = None if lang in ("", "auto") else lang
        self.beam_size = int(cfg.get("beam_size", 1))
        self.vad_filter = bool(cfg.get("vad_filter", True))
        self._model = None
        self.device: str | None = None
        self.compute_type: str | None = None

    # -- loading ---------------------------------------------------------
    def _compute_for(self, device: str) -> str:
        if self.compute_pref != "auto":
            return self.compute_pref
        return "int8_float16" if device == "cuda" else "int8"

    def load(self):
        """Instantiate the model, falling back cuda -> cpu. Returns the model."""
        if self._model is not None:
            return self._model
        from faster_whisper import WhisperModel

        if self.device_pref == "auto":
            order = ["cuda", "cpu"] if _cuda_available() else ["cpu"]
        else:
            order = [self.device_pref] + (["cpu"] if self.device_pref != "cpu" else [])

        last: Exception | None = None
        for device in order:
            compute = self._compute_for(device)
            try:
                self._model = WhisperModel(self.model_name, device=device,
                                           compute_type=compute)
            except Exception as exc:                      # noqa: BLE001
                log.warning("could not load %s on %s/%s: %s",
                            self.model_name, device, compute, exc)
                last = exc
                continue
            self.device, self.compute_type = device, compute
            log.info("STT ready: %s on %s (%s)", self.model_name, device, compute)
            return self._model
        raise RuntimeError(f"could not load Whisper model {self.model_name!r}: {last}")

    # -- inference -------------------------------------------------------
    def transcribe(self, pcm: np.ndarray) -> Transcript:
        """float32 mono PCM at 16 kHz -> text. Empty audio -> empty text."""
        if pcm is None or pcm.size == 0:
            return Transcript("", self.language or "")
        model = self.load()
        segments, info = model.transcribe(
            pcm.astype(np.float32),
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
        )
        # faster-whisper streams lazily; this is where the work happens.
        text = " ".join(s.text.strip() for s in segments).strip()
        lang = getattr(info, "language", None) or self.language or ""
        log.info("STT [%s] %r", lang, text)
        return Transcript(text=text, language=lang,
                          duration_s=float(getattr(info, "duration", 0.0) or 0.0))
