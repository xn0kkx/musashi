"""Builds the effector daemon's tool table.

This is the only Registry in the system that is reachable from outside the
guest process that owns it. Everything a proposer (voice, LLM, a human with
socat) can ever cause to happen is in this file, and the schema here — not the
caller — is what decides whether it happens.

The UI effectors reuse `musashi_gestures.injector.TouchInjector` and
`musashi_gestures.sequencer.TouchSequencer` verbatim. They are created lazily,
on first use, for two reasons: the daemon should not register a second uinput
touchscreen on a system where nobody ever calls ui.tap/shell.swipe, and it
must still start cleanly on a host or headless chroot where /dev/uinput is not
available (in that case the failure surfaces as a rejected intent, not as a
daemon that refuses to boot).

Note this is a *separate* TouchInjector from the gesture engine's. The two
processes cannot share one, and the engine's live-touch slot 0 vs the
sequencer's slot 1 convention only holds within a single device. Synthetic
sequences from here therefore land on their own virtual touchscreen, which
phoc treats as just another input device.
"""
from __future__ import annotations

import logging

from musashi_gestures.intents import Arg, Registry, ToolClass

from .apps import AppManager
from .shell import Shell

log = logging.getLogger("musashi-effector.registry")

SWIPE_DIRS = ("up", "down", "left", "right")


class UiEffectors:
    """Lazily-constructed touch stack shared by shell.swipe and ui.tap."""

    def __init__(self, touch_cfg: dict | None = None):
        self._touch_cfg = touch_cfg or {}
        self._touch = None
        self._seq = None

    @property
    def sequencer(self):
        if self._seq is None:
            from musashi_gestures.injector import TouchInjector
            from musashi_gestures.sequencer import TouchSequencer
            self._touch = TouchInjector()
            self._seq = TouchSequencer(self._touch, **self._touch_cfg)
            log.info("touch stack created (uinput)")
        return self._seq

    def swipe(self, dir: str) -> str:
        self.sequencer.edge_swipe(dir)
        return dir

    def tap(self, x: float, y: float) -> dict:
        self.sequencer.tap(x, y)
        return {"x": x, "y": y}

    def close(self) -> None:
        if self._seq is not None:
            self._seq.stop()
        if self._touch is not None:
            self._touch.close()
        self._seq = self._touch = None


def build_registry(cfg: dict, gate=None) -> tuple[Registry, AppManager, UiEffectors]:
    """cfg is the merged effector.toml. Returns the registry and the two
    resource owners so the caller can shut them down."""
    allow = tuple(cfg.get("apps", {}).get("allow", ()))
    if not allow:
        log.warning("app allowlist is empty — app.launch will reject everything")

    apps = AppManager()
    ui = UiEffectors(cfg.get("touch"))
    reg = Registry(gate=gate)

    reg.register(
        "app.launch", ToolClass.EFFECT,
        {"id": Arg(str, choices=allow)},
        apps.launch,
        doc="Launch an allowlisted .desktop application.",
        destructive=False,
    )
    reg.register(
        "app.close", ToolClass.EFFECT,
        {"id": Arg(str, choices=allow)},
        apps.close,
        doc="SIGTERM an application this daemon launched.",
        # The one tool here that can lose user work. V2's confirmation gate
        # keys off exactly this flag.
        destructive=True,
    )
    reg.register(
        "shell.swipe", ToolClass.EFFECT,
        {"dir": Arg(str, choices=SWIPE_DIRS)},
        ui.swipe,
        doc="Edge swipe: home / notifications / app pages.",
        # EFFECT because it drives the UI, but not destructive: the class is
        # binary by design (Plano Diretor §2.7) and there is no "mostly
        # harmless" third class to escape into. It is navigation — the worst
        # case is a wrong screen, which the next swipe undoes — so it is
        # marked non-destructive and V2 should not demand confirmation for it.
        destructive=False,
    )
    reg.register(
        "ui.tap", ToolClass.EFFECT,
        # Bounded here, unlike the gesture engine's in-process registry: these
        # coordinates arrive from off-process and must not be trusted to be
        # normalized just because the injector happens to clamp them.
        {"x": Arg(float, min=0.0, max=1.0), "y": Arg(float, min=0.0, max=1.0)},
        ui.tap,
        doc="Tap at a normalized [0,1] screen position.",
        destructive=False,
    )
    reg.register(
        "sys.tools", ToolClass.QUERY,
        {},
        reg.describe,
        doc="List the tools this daemon exposes, with their schemas.",
    )

    # shell.exec is off unless the profile explicitly turns it on. `command` is
    # a free-form Arg (no `choices`), so the V1 grammar skips it — only the LLM
    # ever proposes it — and EFFECT + destructive=True routes it through the
    # Registry.gate() where V2's confirmation / prefix-allowlist guard-rails
    # will live. See shell.py for the full trade-off. HOOK(guard-rails S7-S10).
    if cfg.get("shell", {}).get("enabled", False):
        shell = Shell(cfg.get("shell"))
        reg.register(
            "shell.exec", ToolClass.EFFECT,
            {"command": Arg(str)},
            shell.exec_,
            doc="Run a shell command and return {rc, stdout, stderr}.",
            destructive=True,
        )
        log.warning("shell.exec ENABLED — this profile exposes an unrestricted shell")

    return reg, apps, ui
