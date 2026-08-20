"""Per-stage latency instrumentation.

The plan is explicit that this is not optional: "Instrumentar e logar latência
por estágio (VAD, STT, intent, vsock, efeito) desde o primeiro dia — o Plano
Diretor §8/S4 cobra p50 <= 900 ms e sem instrumentação por estágio não há como
otimizar."

Design notes:
  - `perf_counter` is injectable so the tests can assert on exact numbers.
  - A stage that raises still gets closed and recorded, then the exception
    propagates: a failed run's timings are the interesting ones.
  - Stages are recorded in completion order and may be absent (a MISS never
    reaches vsock), so the summary prints what happened, not a fixed template.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

log = logging.getLogger("musashi-voice.latency")

# The budget the Plano Diretor §8/S4 will eventually enforce, end to end.
BUDGET_MS = 900.0


@dataclass(frozen=True)
class Span:
    name: str
    t0: float
    t1: float

    @property
    def ms(self) -> float:
        return (self.t1 - self.t0) * 1000.0


@dataclass
class LatencyTrace:
    """One spoken interaction's timeline."""
    label: str = "utterance"
    clock: object = time.perf_counter
    _spans: list[Span] = field(default_factory=list)
    _marks: list[tuple[str, float]] = field(default_factory=list)
    _t0: float | None = None

    def __post_init__(self) -> None:
        self._t0 = self.clock()

    @contextmanager
    def stage(self, name: str):
        start = self.clock()
        try:
            yield
        finally:
            self._spans.append(Span(name, start, self.clock()))

    def mark(self, name: str) -> None:
        """A zero-length event (e.g. 'recording-stopped')."""
        self._marks.append((name, self.clock()))

    @property
    def spans(self) -> tuple[Span, ...]:
        return tuple(self._spans)

    @property
    def marks(self) -> tuple[tuple[str, float], ...]:
        return tuple(self._marks)

    def get(self, name: str) -> float | None:
        """Milliseconds spent in `name`, or None if the stage never ran."""
        for s in self._spans:
            if s.name == name:
                return s.ms
        return None

    @property
    def total_ms(self) -> float:
        return (self.clock() - self._t0) * 1000.0

    @property
    def measured_ms(self) -> float:
        """Sum of the stages, i.e. the part of the wall clock we can explain.

        Deliberately separate from total_ms, which also counts the glue
        between stages."""
        return sum(s.ms for s in self._spans)

    def format_summary(self) -> str:
        if not self._spans:
            return f"[{self.label}] no stages recorded"
        width = max(len(s.name) for s in self._spans)
        lines = [f"[{self.label}] latency by stage:"]
        lines += [f"  {s.name:<{width}}  {s.ms:8.1f} ms" for s in self._spans]
        lines.append(f"  {'MEASURED':<{width}}  {self.measured_ms:8.1f} ms"
                     f"  (budget {BUDGET_MS:.0f} ms)")
        # The trace is started at the *end* of recording, so WALL is the
        # user-perceived latency: the silence between "I stopped talking" and
        # "the machine answered".
        lines.append(f"  {'WALL':<{width}}  {self.total_ms:8.1f} ms")
        return "\n".join(lines)

    def log_summary(self, level: int = logging.INFO) -> None:
        log.log(level, "%s", self.format_summary())
