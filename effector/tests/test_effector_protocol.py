#!/usr/bin/env python3
"""Host-side tests for the effector's JSON-lines protocol and its real tool
table — no VM, no vsock, no uinput, no PyGObject.

The touch stack and Gio are both imported lazily (registry.UiEffectors,
apps._gi), so build_registry() and IntentServer.handle_line() are fully
exercisable on a plain host. What is verified here is the property the whole
V0 design rests on: rejection happens in the daemon, before any effector runs.

Style matches gesture-engine/tests/: plain assert + print("name: OK").

Run: python3 effector/tests/test_effector_protocol.py
"""
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))                      # effector/
sys.path.insert(0, str(_HERE.parent.parent / "gesture-engine"))

from musashi_effector.registry import build_registry       # noqa: E402
from musashi_effector.server import IntentServer           # noqa: E402
from musashi_gestures.intents import ToolClass             # noqa: E402

CFG = {"server": {"vsock_port": 5000},
       "apps": {"allow": ["foot.desktop", "org.gnome.Calculator.desktop"]}}


def _server():
    reg, apps, ui = build_registry(CFG)
    return IntentServer(reg, sock=None), reg, ui


def _ask(server, **req) -> dict:
    return json.loads(server.handle_line(json.dumps(req)))


# -- tool table ---------------------------------------------------------
def test_tool_table_matches_v0_scope():
    _, reg, _ = _server()
    assert reg.names() == sorted(
        ["app.launch", "app.close", "shell.swipe", "ui.tap", "sys.tools"])
    # window.focus is deliberately absent, not registered-and-broken.
    assert reg.get("window.focus") is None
    print("test_tool_table_matches_v0_scope: OK")


def test_every_side_effecting_tool_is_classed_effect():
    _, reg, _ = _server()
    for name in ("app.launch", "app.close", "shell.swipe", "ui.tap"):
        assert reg.get(name).cls is ToolClass.EFFECT, name
    assert reg.get("sys.tools").cls is ToolClass.QUERY
    assert reg.get("app.close").destructive is True
    assert reg.get("shell.swipe").destructive is False
    print("test_every_side_effecting_tool_is_classed_effect: OK")


def test_allowlist_comes_from_config_not_the_caller():
    reg, _, _ = build_registry({"apps": {"allow": ["only-this.desktop"]}})
    assert list(reg.get("app.launch").schema["id"].choices) == ["only-this.desktop"]
    print("test_allowlist_comes_from_config_not_the_caller: OK")


# -- protocol -----------------------------------------------------------
def test_query_roundtrip():
    server, _, _ = _server()
    res = _ask(server, tool="sys.tools")
    assert res["ok"] and res["error"] is None
    names = [t["name"] for t in res["result"]]
    assert "app.launch" in names
    print("test_query_roundtrip: OK")


def test_malformed_json_is_a_response_not_a_crash():
    server, _, _ = _server()
    res = json.loads(server.handle_line("{not json"))
    assert not res["ok"] and "bad json" in res["error"]
    print("test_malformed_json_is_a_response_not_a_crash: OK")


def test_non_object_request_is_rejected():
    server, _, _ = _server()
    for line in ("[1,2,3]", '"hello"', "null"):
        res = json.loads(server.handle_line(line))
        assert not res["ok"], line
    print("test_non_object_request_is_rejected: OK")


def test_missing_tool_field_is_rejected():
    server, _, _ = _server()
    assert not _ask(server, args={"id": "foot.desktop"})["ok"]
    assert not json.loads(server.handle_line('{"tool": 7}'))["ok"]
    print("test_missing_tool_field_is_rejected: OK")


def test_bad_args_field_is_rejected():
    server, _, _ = _server()
    res = json.loads(server.handle_line('{"tool": "sys.tools", "args": "id=foot"}'))
    assert not res["ok"] and "must be an object" in res["error"]
    print("test_bad_args_field_is_rejected: OK")


def test_launch_outside_allowlist_is_refused_by_the_daemon():
    server, _, _ = _server()
    # No gi import can possibly have happened: the Registry refuses first.
    res = _ask(server, tool="app.launch", args={"id": "/bin/sh"})
    assert not res["ok"] and "not allowed" in res["error"]
    assert "gi" not in sys.modules
    print("test_launch_outside_allowlist_is_refused_by_the_daemon: OK")


def test_no_shell_exec_exists():
    server, _, _ = _server()
    for tool in ("shell.exec", "os.system", "eval"):
        res = _ask(server, tool=tool, args={"cmd": "id"})
        assert not res["ok"] and "unknown tool" in res["error"]
    print("test_no_shell_exec_exists: OK")


def test_close_of_an_unlaunched_app_fails_cleanly():
    server, _, _ = _server()
    res = _ask(server, tool="app.close", args={"id": "foot.desktop"})
    assert not res["ok"] and "not launched by this daemon" in res["error"]
    print("test_close_of_an_unlaunched_app_fails_cleanly: OK")


def test_source_and_confidence_are_metadata_not_authority():
    server, _, _ = _server()
    res = _ask(server, tool="app.launch", args={"id": "evil.desktop"},
               source="human", confidence=1.0)
    assert not res["ok"], "claiming a trusted source must not bypass the allowlist"
    print("test_source_and_confidence_are_metadata_not_authority: OK")


def test_bad_swipe_direction_never_reaches_the_touch_stack():
    server, _, ui = _server()
    res = _ask(server, tool="shell.swipe", args={"dir": "diagonal"})
    assert not res["ok"] and "not allowed" in res["error"]
    assert ui._seq is None, "the uinput touch stack was created for a bad intent"
    print("test_bad_swipe_direction_never_reaches_the_touch_stack: OK")


def test_out_of_range_tap_never_reaches_the_touch_stack():
    server, _, ui = _server()
    assert not _ask(server, tool="ui.tap", args={"x": 12.0, "y": 0.5})["ok"]
    assert ui._seq is None
    print("test_out_of_range_tap_never_reaches_the_touch_stack: OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
