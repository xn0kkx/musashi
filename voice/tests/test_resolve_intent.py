#!/usr/bin/env python3
"""Tests for the intent cascade seam and the latency instrumentation.

The LLM fallback is deliberately not implemented in V1. What must be true
*now* is that plugging it in later is one argument: these tests pin the seam's
contract — when it is called, what it receives, what happens when it declines,
and what happens when it explodes.

Run: python3 voice/tests/test_resolve_intent.py
"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))                      # voice/

from musashi_voice.grammar import Grammar                  # noqa: E402
from musashi_voice.intent import Intent, resolve_intent    # noqa: E402
from musashi_voice.latency import LatencyTrace             # noqa: E402
from musashi_voice.tts import confirmation_text            # noqa: E402

SYS_TOOLS = [
    {"name": "app.launch", "class": "effect", "destructive": False, "doc": "",
     "args": {"id": {"type": "str", "required": True,
                     "choices": ["foot.desktop", "org.gnome.Calculator.desktop"]}}},
    {"name": "sys.tools", "class": "query", "destructive": False, "doc": "", "args": {}},
]
G = Grammar.from_tool_table(SYS_TOOLS)


class SpyFallback:
    """Stands in for the future llama.cpp resolver."""

    def __init__(self, returns=None, raises=None):
        self.calls = []
        self.returns = returns
        self.raises = raises

    def __call__(self, transcript, grammar):
        self.calls.append((transcript, grammar))
        if self.raises is not None:
            raise self.raises
        return self.returns


# -- stage (a): the grammar ---------------------------------------------
def test_grammar_hit_becomes_an_intent():
    intent = resolve_intent("abrir o terminal", G)
    assert isinstance(intent, Intent)
    assert intent.name == "app.launch"
    assert intent.args == {"id": "foot.desktop"}
    assert intent.source == "voice"
    print("test_grammar_hit_becomes_an_intent: OK")


def test_confidence_carries_the_fuzzy_score_normalized():
    intent = resolve_intent("abrir o terminal", G)
    assert 0.0 < intent.confidence <= 1.0
    assert abs(intent.confidence - 1.0) < 1e-9, "an exact phrase must score 100"
    weaker = resolve_intent("abri o terminal", G)
    assert weaker.confidence < 1.0
    print("test_confidence_carries_the_fuzzy_score_normalized: OK")


def test_empty_transcript_resolves_to_nothing():
    for text in ("", "   ", "\n"):
        assert resolve_intent(text, G) is None
    print("test_empty_transcript_resolves_to_nothing: OK")


# -- stage (b): the seam -------------------------------------------------
def test_miss_without_a_fallback_is_none_not_a_guess():
    assert resolve_intent("me conta uma piada", G) is None
    print("test_miss_without_a_fallback_is_none_not_a_guess: OK")


def test_fallback_is_never_called_on_a_grammar_hit():
    spy = SpyFallback(returns=Intent("app.launch", {"id": "foot.desktop"}))
    resolve_intent("abrir o terminal", G, fallback=spy)
    assert spy.calls == [], "the LLM must not run on the fast path"
    print("test_fallback_is_never_called_on_a_grammar_hit: OK")


def test_fallback_receives_the_transcript_and_the_live_grammar():
    # It gets the grammar, not a copy of the tool list, so the LLM's tool
    # definitions can be derived from the same live sys.tools table.
    spy = SpyFallback()
    resolve_intent("faz o cafe", G, fallback=spy)
    assert len(spy.calls) == 1
    transcript, grammar = spy.calls[0]
    assert transcript == "faz o cafe"
    assert grammar is G
    print("test_fallback_receives_the_transcript_and_the_live_grammar: OK")


def test_fallback_proposal_is_returned_verbatim():
    proposal = Intent("app.launch", {"id": "anything.desktop"},
                      source="llm", confidence=0.4)
    got = resolve_intent("poe alguma coisa na tela", G, fallback=SpyFallback(proposal))
    # Note what is NOT here: validation. The guest's Registry rejects a bad id;
    # the host never gets a veto it could be talked out of.
    assert got is proposal
    print("test_fallback_proposal_is_returned_verbatim: OK")


def test_fallback_declining_is_a_miss_not_an_error():
    assert resolve_intent("nada disso", G, fallback=SpyFallback(None)) is None
    print("test_fallback_declining_is_a_miss_not_an_error: OK")


def test_fallback_crash_is_a_miss_not_a_dead_loop():
    spy = SpyFallback(raises=RuntimeError("llama.cpp segfaulted"))
    assert resolve_intent("qualquer coisa estranha", G, fallback=spy) is None
    print("test_fallback_crash_is_a_miss_not_a_dead_loop: OK")


# -- spoken replies ------------------------------------------------------
def test_replies_are_short_and_never_read_a_desktop_id():
    assert confirmation_text("app.launch", label="terminal") == "abrindo terminal"
    assert confirmation_text("app.launch", label="terminal", locale="en") == "opening terminal"
    assert confirmation_text(None) == "nao entendi"
    assert confirmation_text("app.launch", ok=False,
                             error="app.launch: 'id' value not allowed: '/bin/sh'") \
        == "acao nao permitida"
    assert confirmation_text("app.launch", ok=False,
                             error="effector unreachable: no vsock") \
        == "nao consigo falar com o sistema"
    print("test_replies_are_short_and_never_read_a_desktop_id: OK")


# -- instrumentation -----------------------------------------------------
class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, ms):
        self.t += ms / 1000.0


def test_every_stage_is_timed():
    clock = FakeClock()
    trace = LatencyTrace(label="t", clock=clock)
    for name, ms in (("vad", 10), ("stt", 300), ("intent", 2), ("vsock", 5), ("tts", 80)):
        with trace.stage(name):
            clock.advance(ms)
    assert [s.name for s in trace.spans] == ["vad", "stt", "intent", "vsock", "tts"]
    assert abs(trace.get("stt") - 300.0) < 1e-6
    assert abs(trace.measured_ms - 397.0) < 1e-6
    assert "stt" in trace.format_summary()
    print("test_every_stage_is_timed: OK")


def test_a_stage_that_raises_is_still_recorded():
    clock = FakeClock()
    trace = LatencyTrace(clock=clock)
    try:
        with trace.stage("stt"):
            clock.advance(120)
            raise RuntimeError("model died")
    except RuntimeError:
        pass
    assert abs(trace.get("stt") - 120.0) < 1e-6, "a failed run's timings matter most"
    print("test_a_stage_that_raises_is_still_recorded: OK")


def test_missing_stages_are_absent_not_zero():
    # A MISS never reaches vsock; the summary must not claim it took 0 ms.
    trace = LatencyTrace(clock=FakeClock())
    with trace.stage("intent"):
        pass
    assert trace.get("vsock") is None
    assert "vsock" not in trace.format_summary()
    print("test_missing_stages_are_absent_not_zero: OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
