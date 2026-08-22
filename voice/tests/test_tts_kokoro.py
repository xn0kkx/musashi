#!/usr/bin/env python3
"""Tests for KokoroSpeaker's streaming plumbing, with fake audio.

The pure chunker is covered by test_sentence_chunker; here we check the I/O
half — that speak_stream chunks a token stream into sentences, synthesizes and
plays each one in order, and degrades gracefully when synthesis returns nothing.
A fake `sounddevice` module is injected so no real audio device is touched, and
`_synthesize` is stubbed so no kokoro model is loaded.

Run: python3 voice/tests/test_tts_kokoro.py
"""
import pathlib
import sys
import types

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))                      # voice/

from musashi_voice.tts_kokoro import KokoroSpeaker           # noqa: E402


class FakeStream:
    def __init__(self):
        self.writes = []
        self.started = self.closed = False

    def start(self):
        self.started = True

    def write(self, arr):
        self.writes.append(arr)

    def stop(self):
        pass

    def close(self):
        self.closed = True


def _install_fake_sd():
    """Inject a fake sounddevice into sys.modules; returns (module, stream)."""
    stream = FakeStream()
    fake = types.ModuleType("sounddevice")
    fake.OutputStream = lambda **kw: stream
    fake.play = lambda *a, **k: None
    fake.wait = lambda: None
    sys.modules["sounddevice"] = fake
    return fake, stream


def _restore_sd(saved):
    if saved is None:
        sys.modules.pop("sounddevice", None)
    else:
        sys.modules["sounddevice"] = saved


def test_speak_stream_returns_full_text_and_plays_each_sentence():
    import numpy as np
    saved = sys.modules.get("sounddevice")
    _, stream = _install_fake_sd()
    try:
        spk = KokoroSpeaker({"kokoro_voice": "pf_dora"})
        spk._synthesize = lambda text: np.zeros(4, dtype="float32")
        out = spk.speak_stream(iter(["Oi. ", "Tudo ", "bem?"]))
        assert out == "Oi. Tudo bem?", out
        assert len(stream.writes) == 2, "one write per completed sentence"
    finally:
        _restore_sd(saved)
    print("test_speak_stream_returns_full_text_and_plays_each_sentence: OK")


def test_speak_stream_speaks_the_tail_even_without_final_punctuation():
    import numpy as np
    saved = sys.modules.get("sounddevice")
    _, stream = _install_fake_sd()
    try:
        spk = KokoroSpeaker()
        spk._synthesize = lambda text: np.zeros(4, dtype="float32")
        out = spk.speak_stream(iter(["resposta sem ponto final"]))
        assert out == "resposta sem ponto final"
        assert len(stream.writes) == 1
    finally:
        _restore_sd(saved)
    print("test_speak_stream_speaks_the_tail_even_without_final_punctuation: OK")


def test_failed_synthesis_does_not_write_or_crash():
    saved = sys.modules.get("sounddevice")
    _, stream = _install_fake_sd()
    try:
        spk = KokoroSpeaker()
        spk._synthesize = lambda text: None          # model unavailable
        out = spk.speak_stream(iter(["Uma frase.", " Outra frase."]))
        assert out == "Uma frase. Outra frase."
        assert stream.writes == [], "nothing to play when synthesis fails"
    finally:
        _restore_sd(saved)
    print("test_failed_synthesis_does_not_write_or_crash: OK")


def test_disabled_speaker_consumes_but_stays_silent():
    spk = KokoroSpeaker({"enabled": False})
    out = spk.speak_stream(iter(["não ", "deve ", "falar."]))
    assert out == "não deve falar."
    print("test_disabled_speaker_consumes_but_stays_silent: OK")


def test_feed_and_flush_speak_per_sentence():
    spoken = []
    spk = KokoroSpeaker()
    spk.say = lambda text: spoken.append(text)
    spk.feed("primeira frase. segunda ")
    assert spoken == ["primeira frase."]
    spk.feed("frase aqui.")
    assert spoken == ["primeira frase.", "segunda frase aqui."]
    spk.flush()
    assert spoken == ["primeira frase.", "segunda frase aqui."]   # nothing left
    print("test_feed_and_flush_speak_per_sentence: OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
