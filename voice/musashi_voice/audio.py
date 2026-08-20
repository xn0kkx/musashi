"""Microphone capture (sounddevice) + end trimming (Silero VAD).

V1 activation was **push-to-talk**, not a wake word — PTT is more reliable
than wake word with background conversation, it satisfies Plano Diretor
§2.7's demand for a second non-audio factor, and it neutralises the
DolphinAttack / speaker-replay class by construction. `record_utterance()`
records for exactly as long as an externally-owned event says to; the
`__main__.record_once()` harness (a terminal key) still drives it, and it
remains the default CLI mode — see voice/README.md and ROADMAP.md for why
V2 accepted the trade-off below instead of waiting for a guest PTT gesture.

V2 adds always-on activation (`listen()` below): the guest has no console to
press Enter at, so `musashi-voice.service` now runs continuously and a wake
word ("musashi", checked in `wakeword.py`) replaces the human's second Enter
as the signal that a segment is worth transcribing. Silero VAD, used here
only to trim silence in the PTT path, is what `VadSegmenter` turns into the
turn-taking decision itself in the wake path.

Both sounddevice and silero-vad are imported lazily. This module must remain
importable (and the rest of the package testable) on a machine with no audio
device and no torch.
"""
from __future__ import annotations

import collections
import logging
import threading
from typing import Callable, Iterator

import numpy as np

log = logging.getLogger("musashi-voice.audio")

SAMPLERATE = 16000       # Silero and Whisper both want exactly this
WAKE_BLOCKSIZE = 512     # Silero's streaming API requires exactly this at 16 kHz


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


def _default_speech_prob_fn() -> Callable[[np.ndarray, int], float]:
    """Load Silero and wrap it as `(block, samplerate) -> P(speech)`.

    Raises instead of degrading: `trim_silence()` can shrug off a missing VAD
    and pass audio through untrimmed, because the human already decided the
    turn boundary. `listen()` has no human in the loop — without a VAD there
    is no turn boundary at all, so a missing model is a hard failure here,
    not a silently worse mode."""
    model = _load_vad()
    if model is None:
        raise AudioUnavailable("Silero VAD is required for --wake mode "
                               "(pip install silero-vad) but is not available")

    def _prob(block: np.ndarray, samplerate: int) -> float:
        # Silero's *streaming* API (as opposed to `get_speech_timestamps`,
        # which wants the whole utterance up front): call the model directly,
        # once per block, and it keeps its own recurrent state across calls.
        import torch
        with torch.no_grad():
            return float(model(torch.from_numpy(block), samplerate).item())

    return _prob


# -- always-on capture (wake word mode) -----------------------------------
class VadSegmenter:
    """Turns a stream of (block, speech-probability) into utterances.

    Pure state machine, no I/O — this is what makes it testable with
    synthetic probabilities and no audio hardware (see
    tests/test_vad_segmenter.py). `listen()` below is the thin I/O wrapper
    that feeds it real blocks and real Silero probabilities.

    State machine: IDLE, waiting for `vad_threshold`, buffering nothing but a
    rolling pre-roll; SPEAKING, buffering everything, watching for
    `silence_ms` of continuous silence to close the utterance. A closed
    utterance shorter than `min_speech_ms` of estimated speech is dropped —
    a door latch or a cough is not worth waking Whisper for.
    """

    def __init__(self, cfg: dict | None = None, samplerate: int = SAMPLERATE):
        cfg = cfg or {}
        self.samplerate = samplerate
        self.vad_threshold = float(cfg.get("vad_threshold", 0.5))
        self.silence_ms = float(cfg.get("silence_ms", 700))
        self.min_speech_ms = float(cfg.get("min_speech_ms", 250))
        self.preroll_ms = float(cfg.get("preroll_ms", 300))
        self.max_seconds = float(cfg.get("max_seconds", 30.0))

        preroll_blocks = max(1, int(self.preroll_ms / 1000.0 * samplerate / WAKE_BLOCKSIZE))
        self._preroll: collections.deque = collections.deque(maxlen=preroll_blocks)
        self._speaking = False
        self._chunks: list[np.ndarray] = []
        self._speech_ms = 0.0
        self._silence_ms_run = 0.0
        self._total_frames = 0

    def reset(self) -> None:
        """Drop any in-progress utterance and the pre-roll buffer.

        Called after every dispatched interaction (see `listen()`'s TTS
        pause) so that stale pre-roll audio from before a reply never leaks
        into the next utterance."""
        self._preroll.clear()
        self._speaking = False
        self._chunks = []
        self._speech_ms = 0.0
        self._silence_ms_run = 0.0
        self._total_frames = 0

    def push(self, block: np.ndarray, prob: float) -> np.ndarray | None:
        """Feed one block and its Silero speech probability.

        Returns a finished utterance (float32 mono PCM) when silence closes
        one that met `min_speech_ms`, else None."""
        block_ms = len(block) / self.samplerate * 1000.0
        is_speech = prob >= self.vad_threshold

        if not self._speaking:
            self._preroll.append(block)
            if not is_speech:
                return None
            # Trigger: the utterance starts at the pre-roll, not at this
            # block — Silero crosses threshold a block or two into the word,
            # and for a short wake word like "musashi" that lag is exactly
            # the "mu" we cannot afford to lose.
            self._speaking = True
            self._chunks = list(self._preroll)
            self._preroll.clear()
            self._speech_ms = block_ms
            self._silence_ms_run = 0.0
            self._total_frames = sum(len(c) for c in self._chunks)
            return None

        self._chunks.append(block)
        self._total_frames += len(block)
        if is_speech:
            self._speech_ms += block_ms
            self._silence_ms_run = 0.0
        else:
            self._silence_ms_run += block_ms

        hit_silence = self._silence_ms_run >= self.silence_ms
        hit_max = self._total_frames >= self.max_seconds * self.samplerate
        if hit_max:
            log.warning("hit max_seconds in --wake capture; closing utterance")
        if not (hit_silence or hit_max):
            return None

        utterance = np.concatenate(self._chunks, axis=0).reshape(-1).astype(np.float32)
        speech_ms = self._speech_ms
        self.reset()

        if speech_ms < self.min_speech_ms:
            log.debug("dropping %.0f ms utterance: only %.0f ms of speech (< %.0f ms)",
                      len(utterance) / self.samplerate * 1000.0, speech_ms, self.min_speech_ms)
            return None
        return utterance


def listen(cfg: dict | None = None, wake_cfg: dict | None = None,
          stop: threading.Event | None = None, *,
          source: Callable[[], object] | None = None,
          vad=None) -> Iterator[np.ndarray]:
    """Always-on capture: yields one finished utterance (float32 PCM) at a
    time, forever, until `stop` is set.

    `source` and `vad` are the injection seams — same pattern as
    `EffectorClient.connect` and `LatencyTrace.clock` elsewhere in this
    package — so tests drive this with synthetic blocks and a fake
    probability function instead of real hardware and real torch. `source`,
    if given, must be a zero-arg callable returning an object with
    `.read(n) -> (block, overflowed)` (a `sounddevice.InputStream`-shaped
    object, optionally a context manager); `vad`, if given, replaces the
    Silero-backed prober entirely and must be a plain callable
    `(block, samplerate) -> float in [0, 1]` — deliberately *not* a Silero
    model object, so a test can supply one with no torch installed at all.

    TTS feedback loop: this generator is driven by a `for` loop in __main__
    whose body plays a spoken reply between two `yield`s. On resume it drains
    whatever PortAudio buffered while blocked on `sd.play/sd.wait`, discards
    another `resume_delay_ms` of audio, and resets the segmenter — this is
    what stops the system from hearing its own confirmation and treating it
    as a new command. What is NOT handled: true barge-in (interrupting a
    reply mid-sentence) and acoustic echo cancellation for a longer, noisier
    reply overlapping live capture — that needs an AEC stage the Plano
    Diretor places in S3, not this loop. TODO(S3): AEC.
    """
    cfg = cfg or {}
    wake_cfg = wake_cfg or {}
    samplerate = int(cfg.get("samplerate", SAMPLERATE))
    if int(cfg.get("blocksize", WAKE_BLOCKSIZE)) != WAKE_BLOCKSIZE:
        log.warning("[audio].blocksize ignored in --wake mode; Silero streaming needs %d",
                    WAKE_BLOCKSIZE)
    device = cfg.get("input_device") or None
    resume_delay_ms = float(wake_cfg.get("resume_delay_ms", 250))

    speech_prob = vad if vad is not None else _default_speech_prob_fn()
    sd = None if source is not None else _sd()
    open_stream = source or (lambda: sd.InputStream(
        samplerate=samplerate, channels=int(cfg.get("channels", 1)),
        dtype="float32", blocksize=WAKE_BLOCKSIZE, device=device))

    segmenter = VadSegmenter(wake_cfg, samplerate=samplerate)
    stop = stop or threading.Event()

    stream = open_stream()
    # A real sounddevice.InputStream starts/stops itself as a context manager
    # (like record_utterance's `with stream:` above); a test fake need not
    # implement that protocol at all, hence _noop_ctx.
    with stream if hasattr(stream, "__enter__") else _noop_ctx(stream):
        while not stop.is_set():
            block, overflowed = stream.read(WAKE_BLOCKSIZE)
            if overflowed:
                log.warning("input overflow: dropped audio")
            block = np.asarray(block, dtype=np.float32).reshape(-1)
            prob = speech_prob(block, samplerate)
            utterance = segmenter.push(block, prob)
            if utterance is None:
                continue
            yield utterance
            # Resumed: the consumer's body (STT + wake check + maybe a
            # spoken reply) already ran between this yield and the next
            # loop iteration. Drain what accumulated, wait out the tail
            # of any TTS playback, and start the next utterance clean.
            _drain(stream, samplerate, resume_delay_ms)
            segmenter.reset()


class _noop_ctx:
    """`with` no-op for a stream object with no context-manager protocol
    (the fakes in tests/test_vad_segmenter.py don't need one)."""

    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        return self.stream

    def __exit__(self, *exc):
        return False


def _drain(stream, samplerate: int, resume_delay_ms: float) -> None:
    """Discard buffered/incoming audio for `resume_delay_ms` after a reply,
    so the tail of the TTS playback (and PortAudio's overflow backlog from
    while we were blocked on it) never becomes the next utterance's pre-roll."""
    frames = int(resume_delay_ms / 1000.0 * samplerate)
    if frames <= 0:
        return
    read = getattr(stream, "read_available", None)
    pending = read() if callable(read) else frames
    to_drop = max(frames, int(pending) if isinstance(pending, (int, float)) else frames)
    dropped = 0
    while dropped < to_drop:
        try:
            _block, _overflowed = stream.read(min(WAKE_BLOCKSIZE, to_drop - dropped))
        except Exception as exc:                              # noqa: BLE001
            log.debug("drain stopped early: %s", exc)
            break
        dropped += WAKE_BLOCKSIZE
