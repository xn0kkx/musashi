#!/usr/bin/env python3
"""Grammar tests against a frozen copy of the guest's real `sys.tools` reply.

SYS_TOOLS below is verbatim what the V0 daemon answered over
`socat - VSOCK-CONNECT:3:5000` on the live VM. Keeping the real payload here
(rather than a hand-written fixture) is the point: if the effector's schema
shape ever drifts, these tests are where it shows up, not in the field with a
microphone in your hand.

Run: python3 voice/tests/test_grammar_match.py
"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))                      # voice/

from musashi_voice.grammar import (                        # noqa: E402
    Grammar, aliases_for, normalize)

SYS_TOOLS = [
    {"name": "app.close", "class": "effect", "destructive": True,
     "doc": "SIGTERM an application this daemon launched.",
     "args": {"id": {"type": "str", "required": True,
                     "choices": ["foot.desktop", "org.gnome.Calculator.desktop"]}}},
    {"name": "app.launch", "class": "effect", "destructive": False,
     "doc": "Launch an allowlisted .desktop application.",
     "args": {"id": {"type": "str", "required": True,
                     "choices": ["foot.desktop", "org.gnome.Calculator.desktop"]}}},
    {"name": "shell.swipe", "class": "effect", "destructive": False,
     "doc": "Edge swipe: home / notifications / app pages.",
     "args": {"dir": {"type": "str", "required": True,
                      "choices": ["up", "down", "left", "right"]}}},
    {"name": "sys.tools", "class": "query", "destructive": False,
     "doc": "List the tools this daemon exposes, with their schemas.", "args": {}},
    {"name": "ui.tap", "class": "effect", "destructive": False,
     "doc": "Tap at a normalized [0,1] screen position.",
     "args": {"x": {"type": "float", "required": True, "choices": None},
              "y": {"type": "float", "required": True, "choices": None}}},
]

G = Grammar.from_tool_table(SYS_TOOLS)


def _hit(text, tool, args=None, grammar=G):
    m = grammar.match(text)
    assert m is not None, f"{text!r} was a MISS"
    assert m.tool == tool, f"{text!r} -> {m.tool}, expected {tool}"
    if args is not None:
        assert m.args == args, f"{text!r} -> {m.args}, expected {args}"
    return m


# -- normalization ------------------------------------------------------
def test_normalize_strips_accents_punctuation_and_filler():
    assert normalize("Abrir o Terminal, por favor!") == "abrir terminal"
    assert normalize("Lançar a calculadora") == "lancar calculadora"
    assert normalize("Open the calculator, please") == "open calculator"
    print("test_normalize_strips_accents_punctuation_and_filler: OK")


def test_direction_words_are_never_dropped_as_filler():
    # "para cima" loses "para" but must keep "cima"; "up" must survive intact.
    assert normalize("deslizar para cima") == "deslizar cima"
    assert normalize("swipe up") == "swipe up"
    print("test_direction_words_are_never_dropped_as_filler: OK")


# -- phrase construction ------------------------------------------------
def test_grammar_is_built_from_the_live_table_only():
    assert G.tools() == ["app.close", "app.launch", "shell.swipe", "sys.tools"]
    assert all(p.tool in {t["name"] for t in SYS_TOOLS} for p in G.phrases)
    print("test_grammar_is_built_from_the_live_table_only: OK")


def test_ui_tap_is_skipped_because_floats_are_not_speakable():
    assert "ui.tap" not in G.tools()
    print("test_ui_tap_is_skipped_because_floats_are_not_speakable: OK")


def test_every_phrase_carries_an_allowlisted_value():
    allowed = {"foot.desktop", "org.gnome.Calculator.desktop",
               "up", "down", "left", "right"}
    for p in G.phrases:
        for value in p.arg_dict.values():
            assert value in allowed, f"{p.text!r} proposes {value!r}"
    print("test_every_phrase_carries_an_allowlisted_value: OK")


def test_phrase_texts_are_normalized_and_unique():
    texts = [p.text for p in G.phrases]
    assert len(texts) == len(set(texts))
    assert all(t == normalize(t) for t in texts)
    print("test_phrase_texts_are_normalized_and_unique: OK")


# -- aliases ------------------------------------------------------------
def test_curated_aliases_come_first():
    assert aliases_for("foot.desktop")[0] == "terminal"
    assert "calculadora" in aliases_for("org.gnome.Calculator.desktop")
    print("test_curated_aliases_come_first: OK")


def test_unknown_desktop_ids_still_get_a_spoken_form():
    # A newly allowlisted app must be speakable before anyone edits ALIASES.
    assert "weather" in aliases_for("org.gnome.Weather.desktop")
    assert "text editor" in aliases_for("org.gnome.TextEditor.desktop")
    print("test_unknown_desktop_ids_still_get_a_spoken_form: OK")


def test_label_for_never_reads_a_desktop_id_out_loud():
    assert G.label_for("foot.desktop") == "terminal"
    assert G.label_for("up") == "cima"
    print("test_label_for_never_reads_a_desktop_id_out_loud: OK")


# -- matching, pt-BR ----------------------------------------------------
def test_portuguese_commands():
    _hit("abrir o terminal", "app.launch", {"id": "foot.desktop"})
    _hit("abre a calculadora", "app.launch", {"id": "org.gnome.Calculator.desktop"})
    _hit("fechar o terminal", "app.close", {"id": "foot.desktop"})
    _hit("deslizar para cima", "shell.swipe", {"dir": "up"})
    _hit("desliza para a direita", "shell.swipe", {"dir": "right"})
    _hit("listar comandos", "sys.tools", {})
    print("test_portuguese_commands: OK")


def test_english_commands():
    _hit("open the terminal", "app.launch", {"id": "foot.desktop"})
    _hit("launch calculator", "app.launch", {"id": "org.gnome.Calculator.desktop"})
    _hit("close the calculator", "app.close", {"id": "org.gnome.Calculator.desktop"})
    _hit("swipe left", "shell.swipe", {"dir": "left"})
    _hit("what can you do", "sys.tools", {})
    print("test_english_commands: OK")


def test_matching_tolerates_transcription_error():
    # What faster-whisper actually does to PT-BR on a bad mic.
    _hit("abrir terminau", "app.launch", {"id": "foot.desktop"})
    _hit("abri o terminal", "app.launch", {"id": "foot.desktop"})
    print("test_matching_tolerates_transcription_error: OK")


def test_launch_and_close_are_never_confused():
    assert G.match("abrir o terminal").tool == "app.launch"
    assert G.match("fechar o terminal").tool == "app.close"
    print("test_launch_and_close_are_never_confused: OK")


# -- misses -------------------------------------------------------------
def test_unrelated_speech_is_a_miss():
    for text in ("qual e a previsao do tempo para amanha",
                 "me conta uma piada sobre gatos",
                 "manda mensagem para o joao",
                 "coloca uma musica",
                 # The tightest one in the corpus (62.1): near-homophone of
                 # "desliza", and the reason the scorer is fuzz.ratio.
                 "desliga a luz da sala",
                 "what is the capital of mongolia",
                 ""):
        assert G.match(text) is None, f"{text!r} should not have matched"
    print("test_unrelated_speech_is_a_miss: OK")


def test_padded_commands_still_match():
    _hit("abrir a calculadora agora", "app.launch",
         {"id": "org.gnome.Calculator.desktop"})
    _hit("por favor abre o terminal", "app.launch", {"id": "foot.desktop"})
    print("test_padded_commands_still_match: OK")


def test_threshold_is_honoured():
    assert G.match("abrir o terminal", min_score=99.5) is not None
    assert G.match("abrr trmnl", min_score=99.5) is None
    print("test_threshold_is_honoured: OK")


def test_empty_tool_table_matches_nothing():
    assert Grammar.from_tool_table([]).match("abrir o terminal") is None
    print("test_empty_tool_table_matches_nothing: OK")


def test_empty_allowlist_produces_no_launchable_phrases():
    tools = [{"name": "app.launch", "class": "effect",
              "args": {"id": {"type": "str", "required": True, "choices": []}}}]
    assert Grammar.from_tool_table(tools).phrases == ()
    print("test_empty_allowlist_produces_no_launchable_phrases: OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
