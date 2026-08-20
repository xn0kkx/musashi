#!/usr/bin/env python3
"""The daemon serves two sockets (vsock for the host, a unix socket for the
guest's own musashi-voice) over ONE registry. These tests pin the property that
makes that safe: both listeners serialize on the same lock.

Why it matters: the effectors behind the registry are not thread-safe with
respect to each other (TouchSequencer owns a uinput device and a queue,
AppManager mutates a pid dict). Before the second listener existed, IntentServer
minted its own lock in __init__ — correct for one server, and silently useless
for two, which is exactly the kind of bug that only shows up as a mangled touch
sequence under a race nobody can reproduce.

No sockets and no VM: dispatch is driven straight through handle_line() from
two threads, with a handler that sleeps long enough to make overlap certain if
the lock were not shared. Style matches the other suites: plain assert +
print("name: OK").

Run: python3 effector/tests/test_dual_listener_lock.py
"""
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import threading
import time

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))                      # effector/
sys.path.insert(0, str(_HERE.parent.parent / "gesture-engine"))

from musashi_effector.server import IntentServer            # noqa: E402
from musashi_gestures.intents import Registry, ToolClass     # noqa: E402

HOLD_S = 0.05


def _overlap_registry():
    """A registry whose one tool sleeps and records concurrent entries."""
    reg = Registry()
    state = {"inside": 0, "max_inside": 0, "calls": 0}
    guard = threading.Lock()

    def slow():                       # Registry calls handlers as handler(**args)
        with guard:
            state["inside"] += 1
            state["calls"] += 1
            state["max_inside"] = max(state["max_inside"], state["inside"])
        time.sleep(HOLD_S)
        with guard:
            state["inside"] -= 1
        return {"ok": True}

    reg.register("test.slow", schema={}, handler=slow, cls=ToolClass.QUERY,
                 doc="sleeps, to make a race observable")
    return reg, state


def _hammer(servers, per_server=4):
    threads = []
    for server in servers:
        for _ in range(per_server):
            threads.append(threading.Thread(
                target=server.handle_line, args=(json.dumps({"tool": "test.slow"}),)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# -- the property ---------------------------------------------------------
def test_two_servers_sharing_a_lock_serialize_dispatch():
    reg, state = _overlap_registry()
    lock = threading.Lock()
    a = IntentServer(reg, sock=None, lock=lock)
    b = IntentServer(reg, sock=None, lock=lock)
    assert a._lock is b._lock, "the two listeners must hold the same lock object"

    _hammer([a, b])
    assert state["calls"] == 8, state
    assert state["max_inside"] == 1, (
        f"dispatch overlapped ({state['max_inside']} handlers at once) — "
        "the two listeners are not serializing against each other")
    print("test_two_servers_sharing_a_lock_serialize_dispatch: OK")


def test_independent_servers_really_can_overlap():
    """The negative control. Without this, the test above would still pass if
    handle_line() happened to be serialized by something else entirely (the
    GIL, an accidental global) and would be proving nothing about the lock."""
    reg, state = _overlap_registry()
    a = IntentServer(reg, sock=None)          # each mints its own lock
    b = IntentServer(reg, sock=None)
    assert a._lock is not b._lock

    _hammer([a, b])
    assert state["max_inside"] > 1, (
        "two independently-locked servers did not overlap; this test can no "
        "longer distinguish a shared lock from an unshared one")
    print("test_independent_servers_really_can_overlap: OK")


def test_a_lone_server_still_locks_by_default():
    """The default path must stay correct with no caller ceremony."""
    reg, state = _overlap_registry()
    solo = IntentServer(reg, sock=None)
    _hammer([solo], per_server=4)
    assert state["max_inside"] == 1, state
    print("test_a_lone_server_still_locks_by_default: OK")


# -- the wiring -----------------------------------------------------------
def test_unix_socket_is_not_world_connectable():
    """The local socket is a capability endpoint reachable by any process that
    can open it, so its mode is part of the security boundary, not cosmetics."""
    reg, _ = _overlap_registry()
    # Nested path on purpose: on_unix() must create the parent, because in the
    # image that parent is /run/musashi and by hand it may not exist at all.
    path = os.path.join(tempfile.mkdtemp(), "sub", "effector.sock")
    server = IntentServer.on_unix(reg, path)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert not mode & stat.S_IROTH and not mode & stat.S_IWOTH, oct(mode)
        assert mode & stat.S_IRUSR and mode & stat.S_IWUSR, oct(mode)
    finally:
        server.stop()
    print("test_unix_socket_is_not_world_connectable: OK")


def test_dual_listen_is_the_default_and_opt_outable():
    """--no-unix and --unix are the two escape hatches from dual listening;
    assert they exist, since the systemd unit and the docs both rely on the
    default being 'both'."""
    argv = [sys.executable, "-m", "musashi_effector", "--help"]
    env = dict(os.environ,
               PYTHONPATH=os.pathsep.join([str(_HERE.parent),
                                           str(_HERE.parent.parent / "gesture-engine")]))
    out = subprocess.run(argv, capture_output=True, text=True, env=env).stdout
    for flag in ("--no-unix", "--unix-path", "--unix"):
        assert flag in out, f"{flag} missing from the CLI"
    print("test_dual_listen_is_the_default_and_opt_outable: OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
