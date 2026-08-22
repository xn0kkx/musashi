"""Kokoro-82M TTS with sentence-streaming — the natural-voice upgrade to Piper.

Piper (tts.py) speaks one short canned confirmation and is perfect for that.
The LLM assistant is different: it streams prose, sentence by sentence, and the
point is to *start speaking the first sentence while the model is still writing
the second*. That is what keeps a long spoken answer from feeling like it
stalled — first audio out in well under a second, then a continuous voice.

Backed by `kokoro-onnx` (onnxruntime), NOT the torch `kokoro` package: the
latter caps Python at <3.13, and both this Kali host and the Debian 13 guest
run 3.13. kokoro-onnx runs the same 82M model through ORT with no torch, uses
espeak-ng for the Portuguese G2P, and needs two model files (kokoro-v1.0.onnx +
voices-v1.0.bin) whose paths are config (see [tts].kokoro_model/kokoro_voices).

Two pieces, split so the hard part is testable with no audio hardware:

  * `split_sentences(buffer)` is pure: accumulated text in, (complete
    sentences, remainder) out. Same shape as audio.VadSegmenter — a state
    machine you can unit-test exhaustively.
  * `KokoroSpeaker` is the I/O half: a producer thread pulls sentences off a
    queue and synthesizes each with Kokoro, a consumer writes the resulting PCM
    into one continuous sounddevice OutputStream so there is no gap between
    sentences.

Like Speaker, every failure path is non-fatal: no espeak-ng, no model files, no
kokoro-onnx package, no output device — all degrade to printing the line, never
to a crashed voice loop.
"""
from __future__ import annotations

import logging
import queue
import threading

log = logging.getLogger("musashi-voice.tts_kokoro")

SAMPLE_RATE = 24000            # Kokoro's native output rate
_SENTENCE_END = ".!?…"
# Emit a partial clause at a comma once the pending text passes this many
# characters, so a long run-on sentence still starts speaking promptly instead
# of waiting for a full stop that may be seconds of generation away.
_SOFT_COMMA_LEN = 60
_MIN_SENTENCE = 2              # don't emit a lone "." or a stray token

# Abbreviations whose trailing dot must NOT end a sentence. Compared
# lower-cased against the last whitespace-delimited token before the dot.
_ABBREVIATIONS = {
    "sr", "sra", "srta", "dr", "dra", "prof", "profa", "ex", "exmo",
    "av", "r", "n", "pág", "pag", "fig", "etc", "vs", "ltda", "cia",
}


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """Split accumulated streamed text into (complete sentences, remainder).

    Pure and deterministic. The remainder is whatever comes after the last
    boundary — hold it and prepend it to the next call's input as more tokens
    arrive. A newline always closes a sentence; a comma only does once the
    pending clause is already long."""
    sentences: list[str] = []
    start = 0
    i = 0
    n = len(buffer)
    while i < n:
        ch = buffer[i]
        boundary = False
        if ch == "\n":
            boundary = True
        elif ch in _SENTENCE_END:
            # A run of ".!?…" (e.g. "?!" or "...") ends together, and the next
            # char must be whitespace/end — "3.14" and "musashi.abrir" are not
            # boundaries.
            nxt = buffer[i + 1] if i + 1 < n else ""
            if nxt == "" or nxt.isspace():
                if not _ends_with_abbreviation(buffer, start, i):
                    boundary = True
        elif ch == ",":
            if (i - start) >= _SOFT_COMMA_LEN:
                boundary = True

        if boundary:
            piece = buffer[start:i + 1].strip()
            if len(piece) >= _MIN_SENTENCE:
                sentences.append(piece)
                start = i + 1
            # else: too short to be worth speaking; roll it into the next piece
        i += 1

    return sentences, buffer[start:]


def _ends_with_abbreviation(buffer: str, start: int, dot: int) -> bool:
    """Is the dot at `buffer[dot]` the period of a known abbreviation?"""
    seg = buffer[start:dot]
    token = seg.rsplit(None, 1)[-1] if seg.split() else ""
    return token.lower() in _ABBREVIATIONS


class KokoroSpeaker:
    """Kokoro voice, streamed by sentence. Drop-in for Speaker (has say()).

    speak_stream()/feed()/flush() are the streaming path the LLM assistant
    uses; say() is the one-shot path so this speaker also works for the canned
    confirmations, making [tts].engine a clean either/or with Piper."""

    DEFAULT_MODEL = "/opt/musashi/kokoro/kokoro-v1.0.onnx"
    DEFAULT_VOICES = "/opt/musashi/kokoro/voices-v1.0.bin"

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.voice = str(cfg.get("kokoro_voice", "pf_dora"))
        self.lang = str(cfg.get("kokoro_lang", "pt-br"))        # espeak language
        self.speed = float(cfg.get("kokoro_speed", 1.0))
        self.model_path = str(cfg.get("kokoro_model", self.DEFAULT_MODEL))
        self.voices_path = str(cfg.get("kokoro_voices", self.DEFAULT_VOICES))
        self.locale = str(cfg.get("locale", "pt"))
        self._k = None
        self._broken = False
        self._buffer = ""                                       # for feed()/flush()

    # -- model ----------------------------------------------------------
    def _load(self):
        if self._k is not None or self._broken:
            return self._k
        try:
            from kokoro_onnx import Kokoro
            self._k = Kokoro(self.model_path, self.voices_path)
            log.info("Kokoro ready: voice=%s lang=%s (%s)",
                     self.voice, self.lang, self.model_path)
        except Exception as exc:                                # noqa: BLE001
            log.warning("Kokoro unavailable (%s); replies will be printed", exc)
            self._broken = True
        return self._k

    def _synthesize(self, text: str):
        """text -> float32 numpy array at SAMPLE_RATE (or None on failure)."""
        import numpy as np
        k = self._load()
        if k is None:
            return None
        try:
            samples, _sr = k.create(text, voice=self.voice, speed=self.speed,
                                    lang=self.lang)
        except Exception as exc:                                # noqa: BLE001
            log.warning("Kokoro synthesis failed for %r: %s", text, exc)
            return None
        if samples is None or len(samples) == 0:
            return None
        return np.asarray(samples, dtype=np.float32)

    # -- one-shot (confirmations) ---------------------------------------
    def say(self, text: str) -> None:
        if not text:
            return
        if not self.enabled:
            log.info("TTS disabled; would say %r", text)
            return
        audio = self._synthesize(text)
        if audio is None:
            print(f"[voice] {text}")
            return
        try:
            import sounddevice as sd
            sd.play(audio, SAMPLE_RATE)
            sd.wait()
        except Exception as exc:                                # noqa: BLE001
            log.warning("could not play %r: %s", text, exc)
            print(f"[voice] {text}")

    # -- streaming (LLM assistant) --------------------------------------
    def speak_stream(self, token_iter) -> str:
        """Consume a token iterator, speak sentence-by-sentence as they close,
        and return the full text spoken. Synthesis of the next sentence overlaps
        playback of the current one via a producer thread + a single output
        stream, so there is no audible gap between sentences."""
        if not self.enabled:
            spoken = "".join(token_iter)
            log.info("TTS disabled; would say %r", spoken)
            return spoken

        q: "queue.Queue" = queue.Queue()
        spoken_parts: list[str] = []

        def _producer():
            buf = ""
            for token in token_iter:
                if not token:
                    continue
                buf += token
                done, buf = split_sentences(buf)
                for sentence in done:
                    spoken_parts.append(sentence)
                    q.put(sentence)
            tail = buf.strip()
            if tail:
                spoken_parts.append(tail)
                q.put(tail)
            q.put(None)                                         # end sentinel

        prod = threading.Thread(target=_producer, name="kokoro-produce", daemon=True)
        prod.start()
        self._consume(q)
        prod.join(timeout=1.0)
        return " ".join(spoken_parts)

    def feed(self, text: str) -> None:
        """Incremental alternative to speak_stream for callers that push text.
        Speaks each sentence as it completes; call flush() at the end."""
        self._buffer += text
        done, self._buffer = split_sentences(self._buffer)
        for sentence in done:
            self.say(sentence)

    def flush(self) -> None:
        tail = self._buffer.strip()
        self._buffer = ""
        if tail:
            self.say(tail)

    # -- output ---------------------------------------------------------
    def _consume(self, q: "queue.Queue") -> None:
        """Pull sentences off the queue, synthesize, and play through one
        continuous OutputStream. Falls back to printing if audio is missing."""
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as exc:                                # noqa: BLE001
            log.warning("audio output unavailable (%s); printing instead", exc)
            while True:
                item = q.get()
                if item is None:
                    return
                print(f"[voice] {item}")

        try:
            stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1,
                                     dtype="float32")
            stream.start()
        except Exception as exc:                                # noqa: BLE001
            log.warning("could not open output stream (%s); printing instead", exc)
            while True:
                item = q.get()
                if item is None:
                    return
                print(f"[voice] {item}")

        try:
            while True:
                item = q.get()
                if item is None:
                    break
                audio = self._synthesize(item)
                if audio is None:
                    print(f"[voice] {item}")
                    continue
                stream.write(np.asarray(audio, dtype="float32"))
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:                                   # noqa: BLE001
                pass
