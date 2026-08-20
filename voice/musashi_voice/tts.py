"""Spoken confirmation: Piper voice + the short phrases it says.

Piper (OHF-Voice/piper1-gpl) per the plan: ~15M-param ONNX, ~40 ms to first
audio, ~10x realtime on a desktop CPU, pt_BR medium voices available. Kokoro
is the S4 upgrade, not this phase's problem.

The voice model is **not** downloaded automatically — voice/README.md says
where to get the .onnx + .onnx.json and `[tts].model` points at it. With no
model configured the Speaker degrades to logging the line it would have said,
which keeps the whole loop usable on a fresh checkout.

Replies are deliberately tiny ("abrindo terminal"). A voice assistant that
narrates is a voice assistant you stop using.
"""
from __future__ import annotations

import logging

log = logging.getLogger("musashi-voice.tts")

# Keyed by tool, then locale. `label` is the friendly value name the grammar
# resolved ("foot.desktop" -> "terminal"), so the machine never reads a
# .desktop id out loud.
CONFIRMATIONS: dict[str, dict[str, str]] = {
    "app.launch":  {"pt": "abrindo {label}", "en": "opening {label}"},
    "app.close":   {"pt": "fechando {label}", "en": "closing {label}"},
    "shell.swipe": {"pt": "deslizando para {label}", "en": "swiping {label}"},
    "sys.tools":   {"pt": "estes sao os comandos", "en": "these are the commands"},
}

GENERIC: dict[str, dict[str, str]] = {
    "ok":          {"pt": "pronto", "en": "done"},
    "miss":        {"pt": "nao entendi", "en": "I did not get that"},
    "refused":     {"pt": "acao nao permitida", "en": "action not allowed"},
    "failed":      {"pt": "nao consegui", "en": "that did not work"},
    "unreachable": {"pt": "nao consigo falar com o sistema",
                    "en": "I cannot reach the system"},
}


def _pick(table: dict[str, dict[str, str]], key: str, locale: str) -> str:
    entry = table.get(key)
    if entry is None:
        return ""
    return entry.get(locale) or entry.get("pt") or ""


def confirmation_text(tool: str | None, label: str = "", ok: bool = True,
                      error: str | None = None, locale: str = "pt") -> str:
    """The one short line to speak after an interaction."""
    if tool is None:
        return _pick(GENERIC, "miss", locale)
    if not ok:
        reason = (error or "").lower()
        if "unreachable" in reason or "connection" in reason or "closed" in reason:
            return _pick(GENERIC, "unreachable", locale)
        # The effector's wording for an allowlist / class rejection.
        if "not allowed" in reason or "refused" in reason or "unknown tool" in reason:
            return _pick(GENERIC, "refused", locale)
        return _pick(GENERIC, "failed", locale)
    template = _pick(CONFIRMATIONS, tool, locale) or _pick(GENERIC, "ok", locale)
    return template.format(label=label).strip()


class Speaker:
    """Piper voice, loaded lazily, played on the host's default output.

    Every failure path here is non-fatal: TTS is feedback, and losing feedback
    must never lose the action that already happened in the guest."""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.model_path = str(cfg.get("model", "") or "")
        self.locale = str(cfg.get("locale", "pt"))
        self._voice = None
        self._broken = False

    def _load(self):
        if self._voice is not None or self._broken:
            return self._voice
        if not self.model_path:
            log.info("no [tts].model configured; replies will be printed, not spoken")
            self._broken = True
            return None
        try:
            from piper import PiperVoice
            self._voice = PiperVoice.load(self.model_path)
            log.info("TTS ready: %s", self.model_path)
        except Exception as exc:                          # noqa: BLE001
            log.warning("Piper unavailable (%s); replies will be printed", exc)
            self._broken = True
        return self._voice

    def _synthesize(self, voice, text: str):
        """-> (int16 numpy array, samplerate). Handles both piper1-gpl APIs."""
        import numpy as np
        if hasattr(voice, "synthesize"):
            chunks, rate = [], None
            for chunk in voice.synthesize(text):
                chunks.append(np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16))
                rate = chunk.sample_rate
            if chunks:
                return np.concatenate(chunks), rate
        # piper-tts < 1.3
        raw = b"".join(voice.synthesize_stream_raw(text))
        return np.frombuffer(raw, dtype=np.int16), voice.config.sample_rate

    def say(self, text: str) -> None:
        if not text:
            return
        if not self.enabled:
            log.info("TTS disabled; would say %r", text)
            return
        voice = self._load()
        if voice is None:
            print(f"[voice] {text}")
            return
        try:
            import sounddevice as sd
            audio, rate = self._synthesize(voice, text)
            sd.play(audio, rate)
            sd.wait()
        except Exception as exc:                          # noqa: BLE001
            log.warning("could not speak %r: %s", text, exc)
            print(f"[voice] {text}")
