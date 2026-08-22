#!/usr/bin/env python3
"""Tests for LlmAssistant — the multi-turn agent seam.

Same discipline as test_resolve_intent pins the single-shot fallback: these
pin the agent seam's contract with fakes only, no Ollama, no network, no audio.
What must hold: an EFFECT tool call goes to the effector (never validated
here), a QUERY web tool runs in-process, the loop is bounded by max_turns, an
Ollama failure becomes a spoken apology rather than a crash, and the tool
definitions given to the model derive from the live effector table plus the web
tools — with sys.tools excluded and names underscored.

Run: python3 voice/tests/test_assistant.py
"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))                      # voice/

from musashi_voice.assistant import (                         # noqa: E402
    LlmAssistant, strip_think, _extract_text_tool_calls)

TOOL_TABLE = [
    {"name": "app.launch", "class": "effect", "destructive": False,
     "doc": "Launch an app.",
     "args": {"id": {"type": "str", "required": True,
                     "choices": ["foot.desktop"]}}},
    {"name": "shell.exec", "class": "effect", "destructive": True,
     "doc": "Run a shell command.",
     "args": {"command": {"type": "str", "required": True, "choices": None}}},
    {"name": "sys.tools", "class": "query", "destructive": False, "doc": "",
     "args": {}},
]


class FakeResponse:
    def __init__(self, ok=True, result=None, error=None):
        self.ok, self.result, self.error = ok, result, error


class FakeEffector:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or FakeResponse(result={"rc": 0, "stdout": "x"})

    def call(self, name, args, source="voice", confidence=1.0):
        self.calls.append((name, args, source, confidence))
        return self.response


class FakeSpeaker:
    def __init__(self):
        self.spoken = []

    def speak_stream(self, token_iter):
        text = "".join(token_iter)
        self.spoken.append(text)
        return text

    def say(self, text):
        self.spoken.append(text)


class FakeWebTools:
    def __init__(self):
        self.calls = []

    def handles(self, name):
        return name in ("web.search", "web.fetch")

    def dispatch(self, name, args):
        self.calls.append((name, args))
        return "web results"

    def tool_defs(self):
        return [{"type": "function",
                 "function": {"name": "web_search",
                              "parameters": {"type": "object",
                                             "properties": {}, "required": []}}}]


class FakeOllama:
    """Yields scripted per-turn deltas. `script[i]` is a list of deltas."""

    def __init__(self, script, think=False):
        self.script = script
        self.model = "fake-model"
        self.think = think
        self.turns = 0
        self.seen_messages = []

    def chat(self, messages, tools=None):
        self.seen_messages.append([dict(m) for m in messages])
        turn = self.script[self.turns]
        self.turns += 1
        for delta in turn:
            yield delta


def _tc(name, args):
    return {"function": {"name": name, "arguments": args}}


def test_effect_tool_call_goes_to_the_effector():
    ollama = FakeOllama([
        [{"tool_calls": [_tc("shell_exec", {"command": "ls"})]}],
        [{"content": "pronto"}],
    ])
    eff = FakeEffector()
    spk = FakeSpeaker()
    a = LlmAssistant(ollama, eff, TOOL_TABLE, spk)
    out = a.handle("liste os arquivos")
    assert eff.calls == [("shell.exec", {"command": "ls"}, "llm", 0.5)], eff.calls
    assert out == "pronto"
    assert "pronto" in spk.spoken
    print("test_effect_tool_call_goes_to_the_effector: OK")


def test_query_web_tool_runs_in_process_not_via_effector():
    ollama = FakeOllama([
        [{"tool_calls": [_tc("web_search", {"query": "gatos"})]}],
        [{"content": "aqui está"}],
    ])
    eff = FakeEffector()
    web = FakeWebTools()
    a = LlmAssistant(ollama, eff, TOOL_TABLE, FakeSpeaker(), web_tools=web)
    a.handle("procure gatos")
    assert web.calls == [("web.search", {"query": "gatos"})], web.calls
    assert eff.calls == [], "web.* must not touch the effector"
    print("test_query_web_tool_runs_in_process_not_via_effector: OK")


def test_loop_is_bounded_by_max_turns():
    # Every turn asks for another tool call; max_turns caps the runaway.
    ollama = FakeOllama([
        [{"tool_calls": [_tc("shell_exec", {"command": "echo 1"})]}],
        [{"tool_calls": [_tc("shell_exec", {"command": "echo 2"})]}],
        [{"tool_calls": [_tc("shell_exec", {"command": "echo 3"})]}],
    ])
    eff = FakeEffector()
    a = LlmAssistant(ollama, eff, TOOL_TABLE, FakeSpeaker(), max_turns=2)
    a.handle("faça coisas para sempre")
    assert ollama.turns == 2, ollama.turns
    assert len(eff.calls) == 2
    print("test_loop_is_bounded_by_max_turns: OK")


def test_ollama_error_becomes_a_spoken_apology():
    ollama = FakeOllama([[{"error": "connection refused"}]])
    spk = FakeSpeaker()
    a = LlmAssistant(ollama, FakeEffector(), TOOL_TABLE, spk, locale="pt")
    out = a.handle("qualquer coisa")
    assert "não consegui" in out
    assert out in spk.spoken
    print("test_ollama_error_becomes_a_spoken_apology: OK")


def test_a_plain_answer_ends_the_loop_without_tools():
    ollama = FakeOllama([[{"content": "quatro horas"}]])
    eff = FakeEffector()
    a = LlmAssistant(ollama, eff, TOOL_TABLE, FakeSpeaker())
    out = a.handle("que horas são")
    assert out == "quatro horas"
    assert eff.calls == []
    assert ollama.turns == 1
    print("test_a_plain_answer_ends_the_loop_without_tools: OK")


def test_tool_defs_derive_from_table_plus_web_excluding_sys_tools():
    a = LlmAssistant(FakeOllama([]), FakeEffector(), TOOL_TABLE, FakeSpeaker(),
                     web_tools=FakeWebTools())
    defs = a._tool_defs()
    names = [d["function"]["name"] for d in defs]
    assert "shell_exec" in names
    assert "app_launch" in names
    assert "web_search" in names
    assert "sys.tools" not in names and "sys_tools" not in names
    # dotted internal names are mapped to underscores for the model
    assert all("." not in n for n in names), names
    # app.launch's allowlist becomes an enum, derived from the same table
    launch = next(d for d in defs if d["function"]["name"] == "app_launch")
    assert launch["function"]["parameters"]["properties"]["id"]["enum"] == ["foot.desktop"]
    print("test_tool_defs_derive_from_table_plus_web_excluding_sys_tools: OK")


def test_tool_results_are_fed_back_into_the_conversation():
    ollama = FakeOllama([
        [{"tool_calls": [_tc("shell_exec", {"command": "date"})]}],
        [{"content": "é terça"}],
    ])
    eff = FakeEffector(FakeResponse(result={"rc": 0, "stdout": "Tue"}))
    a = LlmAssistant(ollama, eff, TOOL_TABLE, FakeSpeaker())
    a.handle("que dia é hoje")
    # On the 2nd turn the model must see the tool role message with the result.
    second_turn = ollama.seen_messages[1]
    roles = [m["role"] for m in second_turn]
    assert "tool" in roles, roles
    tool_msg = next(m for m in second_turn if m["role"] == "tool")
    assert "Tue" in tool_msg["content"]
    print("test_tool_results_are_fed_back_into_the_conversation: OK")


def test_strip_think_removes_reasoning_block():
    out = "".join(strip_think(iter(["<think>", "raciocínio", "</think>", "resposta"])))
    assert out == "resposta", out
    print("test_strip_think_removes_reasoning_block: OK")


def test_strip_think_handles_tag_split_across_tokens():
    # The opening tag arrives one character at a time, then the answer.
    tokens = list("<think>segredo</think>") + ["olá ", "mundo"]
    out = "".join(strip_think(iter(tokens)))
    assert out == "olá mundo", out
    print("test_strip_think_handles_tag_split_across_tokens: OK")


def test_strip_think_passes_plain_text_untouched():
    out = "".join(strip_think(iter(["sem ", "tags ", "aqui"])))
    assert out == "sem tags aqui", out
    print("test_strip_think_passes_plain_text_untouched: OK")


def test_assistant_speaks_only_the_answer_not_the_reasoning():
    ollama = FakeOllama([[{"content": "<think>vou pensar</think>a resposta é 42"}]])
    spk = FakeSpeaker()
    a = LlmAssistant(ollama, FakeEffector(), TOOL_TABLE, spk)
    out = a.handle("quanto é")
    assert out == "a resposta é 42", out
    print("test_assistant_speaks_only_the_answer_not_the_reasoning: OK")


def test_extract_text_tool_calls_recovers_calls_emitted_as_text():
    # The 8b/4b abliterated builds write tool calls into content like this.
    got = _extract_text_tool_calls('<tools>{"name": "shell_exec", '
                                   '"arguments": {"command": "date"}}</tools>')
    assert got == [{"function": {"name": "shell_exec",
                                 "arguments": {"command": "date"}}}], got
    # tool_call tag and a ```json fence are recovered too; parameters aliases args
    assert _extract_text_tool_calls('<tool_call>{"name":"x","parameters":{"a":1}}</tool_call>') \
        == [{"function": {"name": "x", "arguments": {"a": 1}}}]
    assert _extract_text_tool_calls("bom dia, tudo certo") == []
    print("test_extract_text_tool_calls_recovers_calls_emitted_as_text: OK")


def test_text_tool_call_is_dispatched_and_not_spoken():
    # Model writes the call as text (no structured tool_calls), then answers.
    ollama = FakeOllama([
        [{"content": '<tools>{"name": "shell_exec", "arguments": {"command": "date"}}</tools>'}],
        [{"content": "são três horas"}],
    ])
    eff = FakeEffector()
    spk = FakeSpeaker()
    a = LlmAssistant(ollama, eff, TOOL_TABLE, spk)
    out = a.handle("que horas são")
    assert eff.calls == [("shell.exec", {"command": "date"}, "llm", 0.5)], eff.calls
    assert out == "são três horas"
    # the raw <tools> markup is filtered out of the spoken audio
    assert not any("<tools>" in s for s in spk.spoken), spk.spoken
    print("test_text_tool_call_is_dispatched_and_not_spoken: OK")


def test_system_prompt_grounds_the_agent_in_a_working_directory():
    a = LlmAssistant(FakeOllama([]), FakeEffector(), TOOL_TABLE, FakeSpeaker(),
                     cwd="/home/user/proj")
    assert "/home/user/proj" in a.system_prompt
    print("test_system_prompt_grounds_the_agent_in_a_working_directory: OK")


def test_no_think_switch_appended_only_when_thinking_is_off():
    off = FakeOllama([[{"content": "ok"}]], think=False)
    LlmAssistant(off, FakeEffector(), TOOL_TABLE, FakeSpeaker()).handle("oi")
    assert off.seen_messages[0][0]["content"].endswith("/no_think")

    on = FakeOllama([[{"content": "ok"}]], think=True)
    LlmAssistant(on, FakeEffector(), TOOL_TABLE, FakeSpeaker()).handle("oi")
    assert not on.seen_messages[0][0]["content"].endswith("/no_think")
    print("test_no_think_switch_appended_only_when_thinking_is_off: OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
