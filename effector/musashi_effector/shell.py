"""shell.exec effector: run a shell command, return its output.

This is the one tool that hands a proposer a genuinely open-ended capability —
`command` is a free-form string, not an allowlisted `choices` value — and it
exists specifically so the local LLM can act as a terminal-driving agent on the
host. It is deliberately gated OFF by default: `build_registry` only registers
it when the effector's config sets `[shell].enabled = true`, so the guest image
and any production profile never expose an unrestricted shell by accident. The
host profile (~/.config/musashi/effector.toml) turns it on.

Being EFFECT + destructive=True is what wires it into the future confirmation
gate: `Registry.dispatch` calls `gate(intent, spec)` for every EFFECT, and V2's
guard-rails (a spoken confirmation, an allowlist of command prefixes, a
second non-audio factor) live there — not in this handler. Nothing here
validates or refuses; the handler runs whatever the Registry let through and
reports the result honestly.
"""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("musashi-effector.shell")

# Bound the reply so a `yes` or a `cat` of a huge file can never blow the
# JSON-lines wire (vsock.MAX_LINE is 1 MiB) or the LLM's context window. The
# command still runs to completion; we just truncate what we send back.
MAX_OUTPUT = 8192


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"


class Shell:
    """Runs commands through the user's shell and captures their output."""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.timeout_s = float(cfg.get("timeout_s", 30.0))

    def exec_(self, command: str) -> dict:
        """`command` has already passed the Registry (and, later, the gate)."""
        log.info("shell.exec: %s", command)
        try:
            proc = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {"rc": None, "stdout": "", "stderr": "",
                    "timed_out": True, "timeout_s": self.timeout_s}
        return {
            "rc": proc.returncode,
            "stdout": _truncate(proc.stdout),
            "stderr": _truncate(proc.stderr),
        }
