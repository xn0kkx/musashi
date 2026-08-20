"""app.launch / app.close effectors, over Gio.DesktopAppInfo.

Why Gio rather than parsing Exec= ourselves: `.desktop` Exec lines carry field
codes (%U, %f, %i, ...), TryExec, Terminal=true, DBusActivatable, and a
startup-notification id. Gio implements all of that correctly and is already
on the system for Phosh. We only use it to *launch* — the allowlist decision
happens in the Registry, upstream of here.

Why `launch_uris_as_manager` rather than `launch`: it reports the child PID
back, which is what makes `app.close` deterministic. Closing by PID we spawned
ourselves needs no Wayland protocol, no window matching, and no heuristics —
it just cannot close a window this daemon did not open, which is the honest
limitation to have in V0.
"""
from __future__ import annotations

import logging
import os
import signal
import time

log = logging.getLogger("musashi-effector.apps")


def _gi():
    """Import PyGObject lazily so this module (and its tests) load on a host
    without python3-gi. Raises at call time, never at import time."""
    import gi
    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gio, GLib
    return Gio, GLib


class AppManager:
    """Launches allowlisted .desktop apps and tracks the PIDs it spawned."""

    def __init__(self):
        self._pids: dict[str, int] = {}

    # -- app.launch -----------------------------------------------------
    def launch(self, id: str) -> dict:
        """`id` has already been checked against the allowlist by the Registry."""
        Gio, GLib = _gi()
        try:
            info = Gio.DesktopAppInfo.new(id)
        except TypeError:
            # PyGObject turns a NULL constructor return into TypeError
            # ("constructor returned NULL") rather than giving back None.
            info = None
        if info is None:
            raise RuntimeError(f"no such .desktop entry installed: {id}")

        pid_box: list[int] = []

        def _on_pid(_appinfo, pid, _data=None):
            pid_box.append(int(pid))

        info.launch_uris_as_manager(
            [], None,
            # DO_NOT_REAP_CHILD keeps the PID valid for us to signal later;
            # we reap it ourselves in close()/reap().
            GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.DO_NOT_REAP_CHILD,
            None, None, _on_pid, None,
        )

        if not pid_box:
            # Happens for DBusActivatable entries, where there is no child of
            # ours at all. Launching still worked; app.close just won't be
            # able to target it.
            log.warning("launched %s but got no pid (D-Bus activated?)", id)
            return {"id": id, "pid": None}

        pid = pid_box[0]
        self._pids[id] = pid
        log.info("launched %s pid=%d", id, pid)
        return {"id": id, "pid": pid}

    # -- app.close ------------------------------------------------------
    def close(self, id: str) -> dict:
        pid = self._pids.get(id)
        if pid is None:
            raise RuntimeError(f"not launched by this daemon: {id}")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._pids.pop(id, None)
            self._reap(pid)
            return {"id": id, "pid": pid, "already_gone": True}

        # Short grace period, then reap. No SIGKILL escalation in V0: a
        # forced kill is a destructive action that V2's confirmation gate
        # should own, not a silent fallback here.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self._reap(pid):
                break
            time.sleep(0.05)
        self._pids.pop(id, None)
        log.info("closed %s pid=%d", id, pid)
        return {"id": id, "pid": pid}

    def _reap(self, pid: int) -> bool:
        """True once the child has been collected (or was never ours)."""
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        return done != 0

    def reap_all(self) -> None:
        """Collect any exited children so they don't linger as zombies."""
        for id, pid in list(self._pids.items()):
            if self._reap(pid):
                self._pids.pop(id, None)


# TODO(V0.1): window.focus(id) via `wlrctl toplevel focus`.
#
# Blocked on verifying that phoc actually advertises the
# wlr-foreign-toplevel-management protocol (Phosh's task switcher suggests it
# does, but that is not proof). Deliberately NOT registered as a tool: an
# EFFECT that exists in the table and always fails is worse than one that is
# honestly absent, because the V1 grammar and the LLM tool schema are both
# generated from Registry.describe().
#
# When enabling: add `wlrctl` to build/chroot-setup.sh, register as
#   ToolClass.EFFECT, {"id": Arg(str, choices=<allowlist>)}, destructive=False
# and fall back to pywayland if wlrctl is unavailable.
