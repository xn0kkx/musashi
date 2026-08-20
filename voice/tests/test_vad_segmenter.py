#!/usr/bin/env python3
"""Tests for the always-on capture path (musashi_voice/audio.py's VadSegmenter
and listen()) -- no hardware, no torch, no VM.

VadSegmenter is a pure state machine over (block, speech-probability) pairs,
so it is driven here with synthetic numpy blocks and scripted probabilities
instead of a real microphone and a real Silero model -- torch is not even
importable on a bare checkout (see voice/pyproject.toml's design note), so
these tests must never import it. `listen()` is exercised the same way, via
its `source=`/`vad=` injection seams (same pattern as
EffectorClient.connect and LatencyTrace.clock).

Run: python3 voice/tests/test_vad_segmenter.py
"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))                      # voice/

import numpy as np                                         # noqa: E402

from musashi_voice.audio import WAKE_BLOCKSIZE, VadSegmenter, listen  # noqa: E402

SR = 16000
BLOCK_MS = WAKE_BLOCKSIZE / SR * 1000.0                     # 32 ms


def _block(value: float = 0.1) -> np.ndarray:
    return np.full(WAKE_BLOCKSIZE, value, dtype=np.float32)


def _cfg(**over) -> dict:
    cfg = dict(vad_threshold=0.5, silence_ms=100, min_speech_ms=50,
              preroll_ms=32, max_seconds=30.0)
    cfg.update(over)
    return cfg


# -- VadSegmenter: basic open/close -----------------------------------------
def test_silence_never_opens_an_utterance():
    seg = VadSegmenter(_cfg())
    for _ in range(20):
        assert seg.push(_block(), 0.05) is None
    print("test_silence_never_opens_an_utterance: OK")


def test_sustained_speech_then_silence_closes_an_utterance():
    seg = VadSegmenter(_cfg(silence_ms=100))
    assert seg.push(_block(), 0.9) is None            # trigger
    for _ in range(5):
        assert seg.push(_block(), 0.9) is None         # still speaking
    # 100ms of silence = ceil(100/32) = 4 blocks
    utt = None
    for _ in range(6):
        utt = seg.push(_block(), 0.05)
        if utt is not None:
            break
    assert utt is not None, "utterance never closed"
    assert isinstance(utt, np.ndarray) and utt.dtype == np.float32
    print("test_sustained_speech_then_silence_closes_an_utterance: OK")


def test_preroll_audio_is_included_in_the_utterance():
    # A distinctive value planted only in the pre-roll blocks must survive
    # into the finished utterance -- this is the "mu" of "musashi" the plan
    # calls out as unaffordable to lose.
    seg = VadSegmenter(_cfg(preroll_ms=3 * BLOCK_MS, silence_ms=100, min_speech_ms=0))
    marker = _block(0.777)
    seg.push(marker, 0.05)             # pre-roll, still idle
    seg.push(_block(0.1), 0.05)        # pre-roll, still idle
    seg.push(_block(0.9), 0.9)         # triggers; pre-roll rolls in

    utt = None
    for _ in range(8):
        utt = seg.push(_block(0.05), 0.05)
        if utt is not None:
            break
    assert utt is not None
    assert np.isclose(utt[0], 0.777), "pre-roll block missing from the utterance"
    print("test_preroll_audio_is_included_in_the_utterance: OK")


def test_brief_noise_below_min_speech_is_dropped():
    # One speech block, immediately followed by enough silence to close --
    # below min_speech_ms, so it must be discarded, not returned.
    seg = VadSegmenter(_cfg(silence_ms=100, min_speech_ms=200))
    seg.push(_block(), 0.9)            # trigger: ~32ms of "speech" so far
    utt = None
    for _ in range(8):
        utt = seg.push(_block(), 0.05)
        if utt is not None:
            break
    assert utt is None, "a blip below min_speech_ms should be dropped"
    print("test_brief_noise_below_min_speech_is_dropped: OK")


def test_max_seconds_forces_a_close():
    seg = VadSegmenter(_cfg(silence_ms=10_000, max_seconds=0.2, min_speech_ms=0))
    seg.push(_block(), 0.9)
    utt = None
    blocks_for_max = int(0.2 * SR / WAKE_BLOCKSIZE) + 2
    for _ in range(blocks_for_max):
        utt = seg.push(_block(), 0.9)      # continuous "speech", never silent
        if utt is not None:
            break
    assert utt is not None, "max_seconds safety net did not fire"
    print("test_max_seconds_forces_a_close: OK")


def test_reset_clears_in_progress_state():
    seg = VadSegmenter(_cfg(silence_ms=1_000))
    seg.push(_block(), 0.9)
    assert seg._speaking is True
    seg.reset()
    assert seg._speaking is False
    assert seg._chunks == []
    assert len(seg._preroll) == 0
    print("test_reset_clears_in_progress_state: OK")


def test_segmenter_can_emit_more_than_one_utterance():
    seg = VadSegmenter(_cfg(silence_ms=100, min_speech_ms=0))
    closed = 0
    for _ in range(2):
        seg.push(_block(), 0.9)
        for _ in range(6):
            if seg.push(_block(), 0.05) is not None:
                closed += 1
                break
    assert closed == 2
    print("test_segmenter_can_emit_more_than_one_utterance: OK")


# -- listen(): the I/O wrapper, driven by fakes -----------------------------
class FakeStream:
    """Yields scripted (block, prob) pairs; loops back to silence forever
    once the script runs out, so a `stop` Event is what ends the generator
    rather than a StopIteration."""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0
        self.reads = 0

    def read(self, n):
        self.reads += 1
        if self.i < len(self.script):
            block, _prob = self.script[self.i]
            self.i += 1
        else:
            block = _block(0.01)
        return block, False

    def read_available(self):
        return 0


def _scripted_vad(script):
    """A `vad=` callable that replays the probabilities from `script` in the
    same order FakeStream replays its blocks -- no torch, no Silero."""
    probs = iter(p for _, p in script)

    def _prob(_block, _sr):
        return next(probs, 0.01)
    return _prob


def _speech_then_silence(speech_blocks=6, silence_blocks=6):
    return ([(_block(0.9), 0.9)] * speech_blocks +
            [(_block(0.01), 0.02)] * silence_blocks)


def test_listen_yields_one_utterance_then_stops():
    import threading
    script = _speech_then_silence()
    stream = FakeStream(script)
    stop = threading.Event()

    utterances = []
    for utt in listen(cfg={"samplerate": SR}, wake_cfg=_cfg(silence_ms=100, min_speech_ms=0),
                      stop=stop, source=lambda: stream, vad=_scripted_vad(script)):
        utterances.append(utt)
        stop.set()          # end the generator right after the first utterance

    assert len(utterances) == 1
    assert utterances[0].dtype == np.float32
    print("test_listen_yields_one_utterance_then_stops: OK")


def test_listen_resets_between_utterances():
    import threading
    # Two speech/silence cycles back to back.
    script = _speech_then_silence() + _speech_then_silence()
    stream = FakeStream(script)
    stop = threading.Event()

    seen = []
    for utt in listen(cfg={"samplerate": SR}, wake_cfg=_cfg(silence_ms=100, min_speech_ms=0,
                                                            resume_delay_ms=0),
                      stop=stop, source=lambda: stream, vad=_scripted_vad(script)):
        seen.append(utt)
        if len(seen) == 2:
            stop.set()
    assert len(seen) == 2, f"expected 2 utterances, got {len(seen)}"
    print("test_listen_resets_between_utterances: OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
