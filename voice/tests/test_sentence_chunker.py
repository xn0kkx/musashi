#!/usr/bin/env python3
"""Tests for tts_kokoro.split_sentences — the pure half of the streaming TTS.

The chunker is what lets the assistant start speaking sentence one while the
model is still writing sentence two, so its behavior on *partial* buffers (the
streaming case) matters as much as on whole paragraphs. No audio, no kokoro
package needed — split_sentences is pure text in, text out.

Run: python3 voice/tests/test_sentence_chunker.py
"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))                      # voice/

from musashi_voice.tts_kokoro import split_sentences        # noqa: E402


def test_splits_on_sentence_enders():
    done, rest = split_sentences("Olá. Tudo bem? Que bom!")
    assert done == ["Olá.", "Tudo bem?", "Que bom!"]
    assert rest == ""
    print("test_splits_on_sentence_enders: OK")


def test_holds_incomplete_tail_as_remainder():
    done, rest = split_sentences("Primeira frase. Segunda pela metade")
    assert done == ["Primeira frase."]
    assert rest == " Segunda pela metade"
    print("test_holds_incomplete_tail_as_remainder: OK")


def test_streaming_across_calls_reconstructs_sentence():
    # Feed the remainder back in with the next tokens, as the speaker does.
    done1, rest1 = split_sentences("Vou abrir o")
    assert done1 == [] and rest1 == "Vou abrir o"
    done2, rest2 = split_sentences(rest1 + " terminal agora.")
    assert done2 == ["Vou abrir o terminal agora."]
    assert rest2 == ""
    print("test_streaming_across_calls_reconstructs_sentence: OK")


def test_decimal_number_is_not_a_boundary():
    done, rest = split_sentences("O valor é 3.14 no total")
    assert done == []
    assert rest == "O valor é 3.14 no total"
    print("test_decimal_number_is_not_a_boundary: OK")


def test_abbreviation_dot_does_not_end_a_sentence():
    done, rest = split_sentences("Falei com o Dr. Silva ontem.")
    assert done == ["Falei com o Dr. Silva ontem."]
    assert rest == ""
    print("test_abbreviation_dot_does_not_end_a_sentence: OK")


def test_newline_always_closes_a_sentence():
    done, rest = split_sentences("linha um\nlinha dois")
    assert done == ["linha um"]
    assert rest == "linha dois"
    print("test_newline_always_closes_a_sentence: OK")


def test_long_clause_breaks_on_comma_for_early_speech():
    long_clause = ("bom dia isso aqui é uma introdução bem comprida que passa "
                   "do limite de caracteres, e continua depois")
    done, rest = split_sentences(long_clause)
    assert len(done) == 1
    assert done[0].endswith(",")
    assert rest.strip() == "e continua depois"
    print("test_long_clause_breaks_on_comma_for_early_speech: OK")


def test_short_comma_clause_is_not_split():
    done, rest = split_sentences("oi, tudo bem")
    assert done == []
    assert rest == "oi, tudo bem"
    print("test_short_comma_clause_is_not_split: OK")


def test_runs_of_punctuation_close_once():
    done, rest = split_sentences("Sério?! Não acredito...")
    assert done == ["Sério?!", "Não acredito..."]
    assert rest == ""
    print("test_runs_of_punctuation_close_once: OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
