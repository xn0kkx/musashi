"""Stage (b) seam: transcript -> Intent | None.

`resolve_intent()` is the cascade the plan describes, with only its first step
built:

    1. deterministic grammar + fuzzy match      <- implemented here (V1)
    2. local LLM with typed tool calls          <- NOT implemented (out of V1)

Step 2 is explicitly out of scope for this phase, so what ships is the *seam*:
`fallback` is a `Callable[[str, Grammar], Intent | None]`. Plugging llama.cpp
in later is writing that callable and passing it — no change to this module,
to the loop, or to the grammar.

The seam has a shape, not just a name, and the shape is the point:

  * the fallback receives the grammar, so the LLM's tool definitions can be
    derived from the same live `sys.tools` table the grammar was built from,
    never from a second, drifting copy;
  * it returns a *proposal*, exactly like the grammar does. Plano Diretor §2.7:
    "O LLM propõe; chid decide." Nothing here validates args — the guest's
    Registry does, and it will reject an LLM's invention the same way it
    rejects a human's typo;
  * `confidence` carries the fuzzy score (0-1) so the V2 confirmation gate has
    a number to threshold on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .grammar import MIN_SCORE, Grammar

log = logging.getLogger("musashi-voice.intent")

# Same shape as musashi_gestures.intents.Intent, deliberately re-declared
# rather than imported: the host package does not depend on the guest package,
# and the wire format (vsock.encode_request) is the real contract between them.
@dataclass(frozen=True)
class Intent:
    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    source: str = "voice"
    confidence: float = 1.0

    def __str__(self) -> str:
        return f"{self.name}({dict(self.args)}) from {self.source} @{self.confidence:.2f}"


IntentFallback = Callable[[str, Grammar], "Intent | None"]


def resolve_intent(transcript: str, grammar: Grammar, min_score: float = MIN_SCORE,
                   fallback: IntentFallback | None = None) -> Intent | None:
    """Grammar first; `fallback` (future LLM) only on a miss; None = give up."""
    if not transcript or not transcript.strip():
        return None

    match = grammar.match(transcript, min_score=min_score)
    if match is not None:
        log.info("grammar hit: %r -> %s %s (score %.1f, %s)",
                 transcript, match.tool, match.args, match.score, match.locale)
        return Intent(name=match.tool, args=match.args, source="voice",
                      confidence=match.score / 100.0)

    if fallback is None:
        log.info("MISS (no fallback wired): %r", transcript)
        return None

    try:
        proposal = fallback(transcript, grammar)
    except Exception as exc:                              # noqa: BLE001
        # A fallback that crashes is a miss, never a crashed voice loop.
        log.error("intent fallback raised: %s", exc)
        return None
    if proposal is None:
        log.info("MISS (fallback declined): %r", transcript)
    else:
        log.info("fallback hit: %r -> %s", transcript, proposal)
    return proposal
