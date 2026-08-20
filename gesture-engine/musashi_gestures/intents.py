"""Modality-agnostic intent layer: Intent + typed tool Registry.

This is the contract that replaces the hardcoded `if act.swipe:` dispatch in
`__main__.py`. An `Intent` is "somebody proposes that tool X runs with args Y";
it says nothing about *who* proposed it. Gestures propose intents today, voice
(V1) proposes them tomorrow, over vsock, through the exact same Registry.

The central rule (Plano Diretor §2.7): **the proposer never validates.** A
caller hands over a name and a bag of args; the Registry owns the tool table,
the schema, the value allowlists and the class gate, and decides. A rejection
is a normal `Result(ok=False)`, not an exception — an LLM proposing garbage is
an expected event, not a crash.

Every tool carries a mandatory, binary class:

  QUERY   reads state, no side effects. Free to call.
  EFFECT  changes the world. Allowlisted, typed, and (from V2) confirmed.

There is deliberately no third class. `destructive` is a separate flag *within*
EFFECT so V2 can require a second non-audio factor for `app.close` without
demanding one for `shell.swipe`; it is not an escape hatch from the class.

No third-party imports here — this module is importable from the gesture
engine, from the effector daemon, and from host-side tests alike.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping


class ToolClass(enum.Enum):
    """Mandatory, binary capability class. See module docstring."""
    QUERY = "query"
    EFFECT = "effect"


@dataclass(frozen=True)
class Intent:
    """A *proposed* action. Carries no authority of its own."""
    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    source: str = "unknown"      # "gesture" | "voice" | "vsock" | ...
    confidence: float = 1.0      # 1.0 for deterministic sources

    def __str__(self) -> str:
        return f"{self.name}({self.args}) from {self.source} @{self.confidence:.2f}"


@dataclass(frozen=True)
class Arg:
    """One argument's schema entry.

    `choices` is how a value allowlist is expressed — `app.launch`'s curated
    set of .desktop ids is just an `Arg(str, choices=[...])`, validated by the
    Registry before the handler ever runs.
    """
    type: type
    required: bool = True
    default: Any = None
    choices: Iterable[Any] | None = None
    min: float | None = None
    max: float | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    cls: ToolClass
    schema: Mapping[str, Arg]
    handler: Callable[..., Any]
    doc: str = ""
    destructive: bool = False    # only meaningful for EFFECT; V2 confirmation gate


@dataclass(frozen=True)
class Result:
    ok: bool
    result: Any = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "result": self.result, "error": self.error}


def _reject(msg: str) -> Result:
    return Result(ok=False, error=msg)


class Registry:
    """The tool table. Owns validation; the only way to run an effector.

    `gate` is an optional policy hook called after the schema validates and
    before the handler runs. It receives (intent, spec) and returns a rejection
    reason, or None to allow. V2 hangs confirmation for destructive EFFECTs
    there without touching this module.
    """

    def __init__(self, gate: Callable[[Intent, ToolSpec], str | None] | None = None):
        self._tools: dict[str, ToolSpec] = {}
        self.gate = gate

    # -- registration ---------------------------------------------------
    def register(self, name: str, cls: ToolClass, schema: Mapping[str, Arg],
                 handler: Callable[..., Any], doc: str = "",
                 destructive: bool = False) -> ToolSpec:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        if not isinstance(cls, ToolClass):
            raise TypeError(f"{name}: class must be a ToolClass, got {cls!r}")
        spec = ToolSpec(name, cls, dict(schema), handler, doc, destructive)
        self._tools[name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict]:
        """Machine-readable tool table — the source the V1 grammar and the
        LLM's JSON-Schema tool definitions are both derived from."""
        return [
            {
                "name": s.name,
                "class": s.cls.value,
                "destructive": s.destructive,
                "doc": s.doc,
                "args": {
                    k: {
                        "type": a.type.__name__,
                        "required": a.required,
                        "choices": list(a.choices) if a.choices is not None else None,
                    }
                    for k, a in s.schema.items()
                },
            }
            for s in (self._tools[n] for n in self.names())
        ]

    # -- validation -----------------------------------------------------
    def validate(self, intent: Intent) -> tuple[ToolSpec | None, dict | str]:
        """Returns (spec, coerced_args) on success, (None, reason) on refusal."""
        spec = self._tools.get(intent.name)
        if spec is None:
            return None, f"unknown tool: {intent.name!r}"

        args = dict(intent.args or {})
        unknown = set(args) - set(spec.schema)
        if unknown:
            return None, f"{intent.name}: unknown args: {sorted(unknown)}"

        coerced: dict[str, Any] = {}
        for key, arg in spec.schema.items():
            if key not in args:
                if arg.required:
                    return None, f"{intent.name}: missing required arg {key!r}"
                coerced[key] = arg.default
                continue
            value = args[key]
            # bool is a subclass of int; never let True slip in as a number.
            if arg.type is not bool and isinstance(value, bool):
                return None, f"{intent.name}: {key!r} must be {arg.type.__name__}, got bool"
            if arg.type is float and isinstance(value, int):
                value = float(value)
            if not isinstance(value, arg.type):
                return None, (f"{intent.name}: {key!r} must be {arg.type.__name__}, "
                              f"got {type(value).__name__}")
            if arg.choices is not None and value not in arg.choices:
                # Deliberately does not echo the allowlist back to the caller.
                return None, f"{intent.name}: {key!r} value not allowed: {value!r}"
            if arg.min is not None and value < arg.min:
                return None, f"{intent.name}: {key!r} below minimum {arg.min}"
            if arg.max is not None and value > arg.max:
                return None, f"{intent.name}: {key!r} above maximum {arg.max}"
            coerced[key] = value
        return spec, coerced

    # -- dispatch -------------------------------------------------------
    def dispatch(self, intent: Intent) -> Result:
        """Validate, gate, run. Never raises for caller-supplied badness."""
        spec, checked = self.validate(intent)
        if spec is None:
            return _reject(str(checked))

        if spec.cls is ToolClass.EFFECT and self.gate is not None:
            reason = self.gate(intent, spec)
            if reason:
                return _reject(f"{intent.name}: refused: {reason}")

        try:
            return Result(ok=True, result=spec.handler(**checked))
        except Exception as exc:                        # noqa: BLE001
            return _reject(f"{intent.name}: handler failed: {exc}")
