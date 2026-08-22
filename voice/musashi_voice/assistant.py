"""The LLM assistant seam: transcript -> agentic tool-call loop -> spoken prose.

This is the second seam the voice loop grew, alongside `intent.resolve_intent`'s
single-shot `fallback`. They are deliberately separate because their shapes are
different:

  * `fallback` proposes ONE intent, the loop dispatches it once, and a canned
    line is spoken. Good for "abrir o terminal" that the grammar just barely
    missed. Its contract is frozen by voice/tests/test_resolve_intent.py, so it
    is left exactly as it was.
  * `LlmAssistant` runs a MULTI-turn loop: the model may search the web, read a
    result, run a shell command, read its output, and only then answer — in
    free Portuguese prose that is spoken as it streams. That does not fit
    `Intent | None`, so it gets its own seam on VoiceLoop.

The one rule both share, and the reason this file dispatches to the effector
rather than doing anything itself: **the proposer never validates.** The model
proposes a tool call; the effector's Registry owns the schema, the allowlist,
the class gate, and the decision. web.* is the exception only because those are
QUERY (no side effects) and so run in-process — a search changes nothing to
guard.

Everything network/LLM here is imported lazily and every failure degrades to a
short spoken apology, never a crashed loop — same discipline as the rest of the
package.
"""
from __future__ import annotations

import json
import logging
import os
import re

from .latency import LatencyTrace

log = logging.getLogger("musashi-voice.assistant")

DEFAULT_SYSTEM_PROMPT = (
    "Você é Musashi, um assistente de voz direto em português do Brasil. "
    "Você controla este computador e tem acesso total ao terminal pela "
    "ferramenta shell_exec e à internet por web_search e web_fetch. "
    "Sempre que uma pergunta puder ser respondida rodando um comando "
    "(versão do kernel, usuário atual, arquivos, processos, data), USE o "
    "shell_exec em vez de dizer que não consegue. Depois de obter o "
    "resultado, responda de forma curta e natural, como numa conversa falada."
)

# Effector arg types (from Registry.describe) -> JSON-Schema types.
_JSON_TYPE = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}

# Ollama/OpenAI function names must be [a-zA-Z0-9_-]; the system's tool names
# use dots (app.launch, shell.exec). Map dots<->underscores at the boundary.
def _llm_name(name: str) -> str:
    return name.replace(".", "_")


# Block markups this filter drops from spoken audio: the model's own reasoning,
# and tool calls emitted as text (see _extract_text_tool_calls) rather than
# through the structured tool_calls field — both must never be read aloud.
_STRIP_BLOCKS = (
    ("<think>", "</think>"),
    ("<tool_call>", "</tool_call>"),
    ("<tools>", "</tools>"),
)


def strip_think(token_iter, blocks=_STRIP_BLOCKS):
    """Stream tokens with any <think>.../<tool_call>.../<tools>... blocks removed.

    The abliterated model emits its chain-of-thought inline as <think> tags
    (and ignores Ollama's `think: false`), and the smaller variants emit tool
    calls as literal <tools>{...}</tools> text — without this the TTS would read
    both aloud. Streaming filter: it holds back only a few characters around a
    possible tag, so the real answer still starts speaking as soon as the model
    writes it. Tags may be split across deltas."""
    opens = [o for o, _ in blocks]
    close_for = {o: c for o, c in blocks}
    waiting = None                                            # close tag we want
    carry = ""
    for token in token_iter:
        if not token:
            continue
        carry += token
        while carry:
            if waiting is None:
                hits = [(carry.find(o), o) for o in opens if carry.find(o) != -1]
                if hits:
                    idx, opentag = min(hits)
                    if idx:
                        yield carry[:idx]
                    carry = carry[idx + len(opentag):]
                    waiting = close_for[opentag]
                    continue
                keep = max((_partial_suffix(carry, o) for o in opens), default=0)
                if keep < len(carry):
                    yield carry[:len(carry) - keep]
                carry = carry[len(carry) - keep:]
                break
            else:
                idx = carry.find(waiting)
                if idx != -1:
                    carry = carry[idx + len(waiting):]
                    waiting = None
                    continue
                keep = _partial_suffix(carry, waiting)
                carry = carry[len(carry) - keep:]
                break
    if waiting is None and carry:
        yield carry


def _partial_suffix(text: str, tag: str) -> int:
    """Length of the longest suffix of `text` that is a prefix of `tag`."""
    limit = min(len(text), len(tag) - 1)
    for n in range(limit, 0, -1):
        if tag.startswith(text[-n:]):
            return n
    return 0


_TEXT_CALL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>"
    r"|<tools>\s*(\{.*?\})\s*</tools>"
    r"|```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL,
)


def _extract_text_tool_calls(text: str) -> list[dict]:
    """Rescue tool calls a model emitted as TEXT instead of structured calls.

    The smaller abliterated qwen3 builds (8b/4b) frequently write
    <tools>{"name": "...", "arguments": {...}}</tools> into content rather than
    filling Ollama's tool_calls field, which otherwise looks like the model
    hallucinating an answer. Parsing them back makes those faster,
    GPU-resident models usable for real tool use. Returns calls in the same
    shape as the structured ones."""
    calls: list[dict] = []
    for m in _TEXT_CALL_RE.finditer(text):
        blob = next((g for g in m.groups() if g), None)
        if not blob:
            continue
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        name = obj.get("name")
        if not name:
            continue
        args = obj.get("arguments", obj.get("parameters", {}))
        calls.append({"function": {"name": name, "arguments": args}})
    return calls


class OllamaClient:
    """Thin streaming client for Ollama's /api/chat. Never raises: a transport
    error surfaces as a single delta carrying an error string the caller speaks."""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.base_url = str(cfg.get("base_url", "http://127.0.0.1:11434")).rstrip("/")
        self.model = str(cfg.get("model", "huihui_ai/qwen3-abliterated:8b"))
        self.keep_alive = cfg.get("keep_alive", "30m")
        self.num_ctx = int(cfg.get("num_ctx", 8192))
        self.temperature = float(cfg.get("temperature", 0.7))
        self.think = bool(cfg.get("think", False))
        self.timeout_s = float(cfg.get("timeout_s", 120.0))

    def chat(self, messages: list[dict], tools: list[dict] | None = None):
        """Yield deltas: {"content": str} and/or {"tool_calls": list}.

        Streams token-by-token so the speaker can start on the first sentence
        while the model is still generating."""
        try:
            import requests
        except ImportError:
            yield {"error": "requests not installed"}
            return

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": self.think,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.num_ctx, "temperature": self.temperature},
        }
        if tools:
            payload["tools"] = tools

        try:
            resp = requests.post(f"{self.base_url}/api/chat", json=payload,
                                 stream=True, timeout=self.timeout_s)
            resp.raise_for_status()
        except Exception as exc:                                # noqa: BLE001
            log.error("ollama chat request failed: %s", exc)
            yield {"error": f"ollama unreachable: {exc}"}
            return

        for line in resp.iter_lines():
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") or {}
            delta: dict = {}
            if msg.get("content"):
                delta["content"] = msg["content"]
            if msg.get("tool_calls"):
                delta["tool_calls"] = msg["tool_calls"]
            if delta:
                yield delta
            if obj.get("done"):
                break


class LlmAssistant:
    """Grammar-miss handler: run the model's tool-call loop and speak the answer."""

    def __init__(self, ollama: OllamaClient, effector, tool_table: list[dict],
                 speaker, *, web_tools=None, system_prompt: str | None = None,
                 max_turns: int = 6, locale: str = "pt", cwd: str | None = None):
        self.ollama = ollama
        self.effector = effector
        self.tool_table = tool_table or []
        self.speaker = speaker
        self.web_tools = web_tools
        # Ground the agent in its real filesystem location: shell.exec runs from
        # here, so without it the model invents placeholder paths like
        # "/path/to/voice" and every relative question fails. Fixes tool use
        # across ALL model sizes, not just the weak ones.
        self.cwd = cwd or os.getcwd()
        base = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.system_prompt = (
            f"{base} O diretório de trabalho atual é {self.cwd}; rode comandos "
            f"com caminhos relativos a ele e nunca invente caminhos como "
            f"/path/to/…."
        )
        self.max_turns = int(max_turns)
        self.locale = locale
        # LLM name (underscored) -> real dotted tool name, for dispatch.
        self._name_map = {_llm_name(t["name"]): t["name"] for t in self.tool_table}

    def warmup(self) -> None:
        """Load the model into memory now, so the first real query does not pay
        the cold-start cost (tens of seconds for a big MoE). Meant to run in a
        background thread at startup, in parallel with the Whisper load; uses
        the configured keep_alive so the model stays resident afterwards."""
        try:
            for _ in self.ollama.chat([{"role": "user", "content": "oi"}], None):
                pass
            log.info("LLM warmed up (%s)", self.ollama.model)
        except Exception as exc:                                # noqa: BLE001
            log.warning("LLM warmup failed: %s", exc)

    # -- tool definitions (single source: the effector table + web tools) --
    def _tool_defs(self) -> list[dict]:
        defs: list[dict] = []
        for t in self.tool_table:
            # sys.tools is the bootstrap query the grammar/assistant were built
            # from; the model has no reason to list tools, so keep it out.
            if t["name"] == "sys.tools":
                continue
            props, required = {}, []
            for arg, spec in (t.get("args") or {}).items():
                jtype = _JSON_TYPE.get(spec.get("type", "str"), "string")
                p: dict = {"type": jtype}
                if spec.get("choices"):
                    p["enum"] = list(spec["choices"])
                props[arg] = p
                if spec.get("required", True):
                    required.append(arg)
            defs.append({
                "type": "function",
                "function": {
                    "name": _llm_name(t["name"]),
                    "description": t.get("doc", ""),
                    "parameters": {"type": "object", "properties": props,
                                   "required": required},
                },
            })
        if self.web_tools is not None:
            defs.extend(self.web_tools.tool_defs())
        return defs

    # -- one tool call -> a short string to feed back to the model ---------
    def _dispatch_tool(self, llm_name: str, args: dict) -> str:
        # HOOK(guard-rails S7-S10): nothing here validates or refuses. QUERY
        # web tools run in-process; everything else goes to the effector, whose
        # Registry.gate() is the single authority on EFFECT actions.
        real = self._name_map.get(llm_name, llm_name.replace("_", "."))
        if self.web_tools is not None and self.web_tools.handles(real):
            return self.web_tools.dispatch(real, args)
        res = self.effector.call(real, dict(args), source="llm", confidence=0.5)
        if res.ok:
            return json.dumps(res.result, ensure_ascii=False, default=str)
        return f"error: {res.error}"

    # -- the loop ----------------------------------------------------------
    def handle(self, transcript: str, trace: LatencyTrace | None = None) -> str:
        """Run the assistant on one transcript. Returns the final spoken text.
        Never raises: any error becomes a short spoken apology."""
        trace = trace or LatencyTrace(label="assistant")
        tools = self._tool_defs()
        # Ollama's `think: false` is ignored by this abliterated build, so use
        # qwen3's textual soft-switch to actually suppress generating the
        # reasoning (strip_think is still the belt-and-suspenders on output).
        # This is the single biggest latency lever for voice.
        system = self.system_prompt
        if not getattr(self.ollama, "think", False):
            system += " /no_think"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": transcript},
        ]

        spoken = ""
        for turn in range(self.max_turns):
            calls: list[dict] = []
            error_box: dict = {}
            raw_parts: list[str] = []

            def _tokens():
                for delta in self.ollama.chat(messages, tools):
                    if delta.get("error"):
                        error_box["error"] = delta["error"]
                        continue
                    if delta.get("tool_calls"):
                        calls.extend(delta["tool_calls"])
                    if delta.get("content"):
                        raw_parts.append(delta["content"])   # unstripped, for parsing
                        yield delta["content"]

            with trace.stage(f"llm-turn-{turn}"):
                spoken = self._speak(strip_think(_tokens()))

            if error_box:
                reply = self._apology()
                self.speaker.say(reply)
                return reply

            # Rescue tool calls the model wrote as text instead of structured
            # calls (the smaller abliterated builds do this constantly).
            if not calls:
                calls = _extract_text_tool_calls("".join(raw_parts))
                if calls:
                    spoken = ""          # it was a tool call, not a real answer

            if not calls:
                # A plain answer with no tool calls ends the loop. If the model
                # produced nothing sayable (it happens after a tool turn on the
                # smaller builds), speak a short confirmation rather than leave
                # the user with silence.
                if not spoken.strip():
                    spoken = self._done_word()
                    self.speaker.say(spoken)
                return spoken

            # Record the assistant's tool-calling turn, then each tool result.
            messages.append({"role": "assistant", "content": spoken,
                             "tool_calls": calls})
            for call in calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                args = self._parse_args(fn.get("arguments"))
                with trace.stage(f"tool:{name}"):
                    result = self._dispatch_tool(name, args)
                messages.append({"role": "tool", "name": name, "content": result})

        log.warning("assistant hit max_turns (%d) without a final answer",
                    self.max_turns)
        return spoken

    # -- helpers -----------------------------------------------------------
    def _speak(self, token_iter) -> str:
        """Stream tokens to the speaker if it can stream; else buffer and say."""
        if hasattr(self.speaker, "speak_stream"):
            return self.speaker.speak_stream(token_iter)
        text = "".join(token_iter)
        if text.strip():
            self.speaker.say(text)
        return text

    @staticmethod
    def _parse_args(arguments) -> dict:
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    def _apology(self) -> str:
        return ("desculpe, não consegui responder agora" if self.locale == "pt"
                else "sorry, I could not answer just now")

    def _done_word(self) -> str:
        return "pronto" if self.locale == "pt" else "done"
