#!/usr/bin/env python3
"""Tests for the wake word gate (musashi_voice/wakeword.py).

No hardware, no VM: `strip_wake_word()` takes a plain string, the same shape
of input the always-on loop hands it after STT. The corpus below is measured,
not guessed — this file prints the exact fuzz.ratio scores it asserts on, so
a future change to rapidfuzz or to DEFAULT_THRESHOLD shows its effect here
first, the same way tests/test_grammar_match.py's scorer table does for the
command grammar.

Run: python3 voice/tests/test_wake_word.py
"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))                      # voice/

from rapidfuzz import fuzz                                 # noqa: E402

from musashi_voice.wakeword import (                       # noqa: E402
    DEFAULT_THRESHOLD, DEFAULT_WAKE_WORD, strip_wake_word)


# -- exact word and command extraction -----------------------------------
def test_wake_word_with_command_strips_to_the_command():
    assert strip_wake_word("musashi abrir o terminal") == "abrir terminal"
    print("test_wake_word_with_command_strips_to_the_command: OK")


def test_wake_word_is_not_case_or_accent_sensitive():
    assert strip_wake_word("Musashi, fechar a calculadora!") == "fechar calculadora"
    print("test_wake_word_is_not_case_or_accent_sensitive: OK")


def test_wake_word_alone_returns_empty_string_not_none():
    # "" means "heard you, no command" -- __main__ answers "não entendi" for
    # this, distinct from silently discarding a MISS.
    result = strip_wake_word("musashi")
    assert result == "", f"expected '', got {result!r}"
    assert result is not None
    print("test_wake_word_alone_returns_empty_string_not_none: OK")


# -- misses ---------------------------------------------------------------
def test_unrelated_speech_is_a_miss():
    for phrase in ("oi tudo bem", "a musica esta tocando muito alto",
                   "vou tomar um mochaccino agora", "que horas sao"):
        assert strip_wake_word(phrase) is None, f"{phrase!r} should be a MISS"
    print("test_unrelated_speech_is_a_miss: OK")


def test_wake_word_mid_sentence_is_not_a_hit():
    # Mentioning "musashi" anywhere in a sentence must not trigger -- only a
    # transcript that *starts* with it does. Otherwise any conversation about
    # this machine becomes a live command feed.
    assert strip_wake_word("eu chamo esse computador de musashi") is None
    print("test_wake_word_mid_sentence_is_not_a_hit: OK")


def test_empty_transcript_is_a_miss():
    assert strip_wake_word("") is None
    assert strip_wake_word("   ") is None
    print("test_empty_transcript_is_a_miss: OK")


# -- phonetic variants a real STT plausibly produces -----------------------
def test_phonetic_variants_still_hit():
    for variant in ("musashe", "musachi", "musaxi", "mussashi"):
        result = strip_wake_word(f"{variant} abrir o terminal")
        assert result == "abrir terminal", f"{variant!r} -> {result!r}"
    print("test_phonetic_variants_still_hit: OK")


def test_split_word_hits_via_the_two_token_window():
    # faster-whisper occasionally breaks a name across a word boundary.
    assert strip_wake_word("mu sashi abrir o terminal") == "abrir terminal"
    print("test_split_word_hits_via_the_two_token_window: OK")


def test_far_mishearings_are_a_documented_limitation():
    # These sit below DEFAULT_THRESHOLD by construction (see the scored
    # corpus in wakeword.py) -- listed here so the limitation is asserted on,
    # not just claimed in a comment.
    for phrase in ("mochachi abrir o terminal", "mudancas de casa outra vez"):
        assert strip_wake_word(phrase) is None, f"{phrase!r} should stay a MISS"
    print("test_far_mishearings_are_a_documented_limitation: OK")


# -- threshold behaviour ----------------------------------------------------
def test_threshold_is_honoured():
    # "musaxi" alone scores below 100 but above the default threshold; a much
    # stricter caller-supplied threshold should reject it.
    assert strip_wake_word("musaxi abrir o terminal") == "abrir terminal"
    assert strip_wake_word("musaxi abrir o terminal", threshold=95.0) is None
    print("test_threshold_is_honoured: OK")


def test_custom_wake_word_is_respected():
    assert strip_wake_word("jarvis abrir o terminal", wake_word="jarvis") == "abrir terminal"
    assert strip_wake_word("musashi abrir o terminal", wake_word="jarvis") is None
    print("test_custom_wake_word_is_respected: OK")


# -- the measured corpus itself, printed and asserted on -------------------
def test_measured_corpus_has_a_clear_gap_above_threshold():
    hits = ["musashi", "mussashi", "musashe", "musachi", "musaxi"]
    misses = ["mustache", "musical", "mochachi", "mudancas", "moçambique", "maquina"]

    scored_hits = [(w, fuzz.ratio(w, DEFAULT_WAKE_WORD)) for w in hits]
    scored_misses = [(w, fuzz.ratio(w, DEFAULT_WAKE_WORD)) for w in misses]
    worst_hit = min(s for _, s in scored_hits)
    best_miss = max(s for _, s in scored_misses)

    print("  wake-word corpus (fuzz.ratio against %r):" % DEFAULT_WAKE_WORD)
    for w, s in scored_hits + scored_misses:
        print(f"    {w:<12} {s:5.1f}")
    print(f"    worst hit  {worst_hit:5.1f}")
    print(f"    best miss  {best_miss:5.1f}")

    assert worst_hit > best_miss, "no gap between hits and misses in this corpus"
    assert best_miss < DEFAULT_THRESHOLD <= worst_hit, (
        f"DEFAULT_THRESHOLD={DEFAULT_THRESHOLD} is not between "
        f"best_miss={best_miss} and worst_hit={worst_hit}")
    print("test_measured_corpus_has_a_clear_gap_above_threshold: OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
