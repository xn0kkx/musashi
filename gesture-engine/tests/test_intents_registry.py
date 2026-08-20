#!/usr/bin/env python3
"""Host-side tests for the intent Registry: schema validation, value
allowlists, and the QUERY/EFFECT class gate.

intents.py imports nothing outside the stdlib, so this runs anywhere with
plain Python 3.11+ — no VM, no root, no uinput. Style matches the other files
in this directory: plain assert + print("name: OK"), no pytest.

Run: python3 gesture-engine/tests/test_intents_registry.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from musashi_gestures.intents import (  # noqa: E402
    Arg, Intent, Registry, ToolClass,
)

ALLOWED_APPS = ("foot.desktop", "org.gnome.Calculator.desktop")


def _registry(gate=None):
    """A stand-in tool table with the same shape as the effector's."""
    calls = []
    reg = Registry(gate=gate)
    reg.register(
        "app.launch", ToolClass.EFFECT,
        {"id": Arg(str, choices=ALLOWED_APPS)},
        lambda id: calls.append(("launch", id)) or {"id": id},
        destructive=False,
    )
    reg.register(
        "ui.tap", ToolClass.EFFECT,
        {"x": Arg(float, min=0.0, max=1.0), "y": Arg(float, min=0.0, max=1.0)},
        lambda x, y: calls.append(("tap", x, y)) or {"x": x, "y": y},
    )
    reg.register(
        "shell.swipe", ToolClass.EFFECT,
        {"dir": Arg(str, choices=("up", "down", "left", "right")),
         "repeat": Arg(int, required=False, default=1)},
        lambda dir, repeat: calls.append(("swipe", dir, repeat)) or dir,
    )
    reg.register(
        "sys.tools", ToolClass.QUERY, {},
        lambda: calls.append(("tools",)) or reg.names(),
    )
    return reg, calls


def _i(name, **args):
    return Intent(name, args, source="test")


# -- unknown tools ------------------------------------------------------
def test_unknown_tool_is_rejected():
    reg, calls = _registry()
    res = reg.dispatch(_i("shell.exec", cmd="rm -rf /"))
    assert not res.ok
    assert "unknown tool" in res.error
    assert calls == []
    print("test_unknown_tool_is_rejected: OK")


# -- schema -------------------------------------------------------------
def test_missing_required_arg_is_rejected():
    reg, calls = _registry()
    res = reg.dispatch(_i("app.launch"))
    assert not res.ok and "missing required arg" in res.error
    assert calls == []
    print("test_missing_required_arg_is_rejected: OK")


def test_unknown_arg_is_rejected():
    reg, calls = _registry()
    res = reg.dispatch(_i("app.launch", id="foot.desktop", env="LD_PRELOAD=evil.so"))
    assert not res.ok and "unknown args" in res.error
    assert calls == []
    print("test_unknown_arg_is_rejected: OK")


def test_wrong_type_is_rejected():
    reg, calls = _registry()
    res = reg.dispatch(_i("app.launch", id=42))
    assert not res.ok and "must be str" in res.error
    assert calls == []
    print("test_wrong_type_is_rejected: OK")


def test_bool_never_passes_as_a_number():
    # bool is a subclass of int in Python; True must not sneak through as 1.
    reg, calls = _registry()
    res = reg.dispatch(_i("ui.tap", x=True, y=0.5))
    assert not res.ok and "got bool" in res.error
    assert calls == []
    print("test_bool_never_passes_as_a_number: OK")


def test_int_is_widened_to_float():
    reg, calls = _registry()
    res = reg.dispatch(_i("ui.tap", x=0, y=1))
    assert res.ok, res.error
    assert calls == [("tap", 0.0, 1.0)]
    print("test_int_is_widened_to_float: OK")


def test_out_of_range_coordinates_are_rejected():
    reg, calls = _registry()
    assert not reg.dispatch(_i("ui.tap", x=1.5, y=0.5)).ok
    assert not reg.dispatch(_i("ui.tap", x=0.5, y=-0.1)).ok
    assert calls == []
    print("test_out_of_range_coordinates_are_rejected: OK")


def test_optional_arg_gets_its_default():
    reg, calls = _registry()
    res = reg.dispatch(_i("shell.swipe", dir="up"))
    assert res.ok and res.result == "up"
    assert calls == [("swipe", "up", 1)]
    print("test_optional_arg_gets_its_default: OK")


# -- allowlist ----------------------------------------------------------
def test_app_outside_allowlist_is_rejected():
    reg, calls = _registry()
    for bad in ("/bin/sh", "evil.desktop", "foot.desktop\n", "FOOT.DESKTOP"):
        res = reg.dispatch(_i("app.launch", id=bad))
        assert not res.ok, bad
        assert "not allowed" in res.error
    assert calls == []
    print("test_app_outside_allowlist_is_rejected: OK")


def test_allowlisted_app_runs():
    reg, calls = _registry()
    res = reg.dispatch(_i("app.launch", id="foot.desktop"))
    assert res.ok and res.result == {"id": "foot.desktop"}
    assert calls == [("launch", "foot.desktop")]
    print("test_allowlisted_app_runs: OK")


def test_rejection_does_not_echo_the_allowlist():
    # The error tells the caller *their* value was refused, not what would
    # have been accepted — no enumeration oracle for a proposer.
    reg, _ = _registry()
    err = reg.dispatch(_i("app.launch", id="evil.desktop")).error
    assert "foot.desktop" not in err
    print("test_rejection_does_not_echo_the_allowlist: OK")


def test_empty_allowlist_rejects_everything():
    reg = Registry()
    reg.register("app.launch", ToolClass.EFFECT, {"id": Arg(str, choices=())},
                 lambda id: id)
    assert not reg.dispatch(_i("app.launch", id="foot.desktop")).ok
    print("test_empty_allowlist_rejects_everything: OK")


# -- QUERY vs EFFECT routing --------------------------------------------
def test_gate_sees_effects_only():
    seen = []

    def gate(intent, spec):
        seen.append(intent.name)
        assert spec.cls is ToolClass.EFFECT
        return None

    reg, calls = _registry(gate=gate)
    assert reg.dispatch(_i("sys.tools")).ok
    assert seen == []                       # QUERY bypasses the gate entirely
    assert reg.dispatch(_i("app.launch", id="foot.desktop")).ok
    assert seen == ["app.launch"]
    assert calls == [("tools",), ("launch", "foot.desktop")]
    print("test_gate_sees_effects_only: OK")


def test_gate_can_refuse_an_effect():
    reg, calls = _registry(gate=lambda intent, spec: "needs confirmation")
    res = reg.dispatch(_i("app.launch", id="foot.desktop"))
    assert not res.ok and "needs confirmation" in res.error
    assert calls == []                      # handler never ran
    assert reg.dispatch(_i("sys.tools")).ok  # QUERY still free
    print("test_gate_can_refuse_an_effect: OK")


def test_gate_runs_after_validation():
    # A malformed intent must never reach the policy hook — validation is
    # cheap and unconditional, policy is not.
    seen = []
    reg, _ = _registry(gate=lambda intent, spec: seen.append(intent.name))
    assert not reg.dispatch(_i("app.launch", id="evil.desktop")).ok
    assert seen == []
    print("test_gate_runs_after_validation: OK")


def test_destructive_flag_is_per_tool_not_per_class():
    reg, _ = _registry()
    assert reg.get("app.launch").cls is ToolClass.EFFECT
    assert reg.get("app.launch").destructive is False
    assert reg.get("sys.tools").cls is ToolClass.QUERY
    print("test_destructive_flag_is_per_tool_not_per_class: OK")


# -- registry hygiene ---------------------------------------------------
def test_class_is_mandatory_and_typed():
    reg = Registry()
    try:
        reg.register("x", "effect", {}, lambda: None)
    except TypeError:
        pass
    else:
        raise AssertionError("a bare string was accepted as a ToolClass")
    print("test_class_is_mandatory_and_typed: OK")


def test_duplicate_registration_is_refused():
    reg, _ = _registry()
    try:
        reg.register("app.launch", ToolClass.QUERY, {}, lambda: None)
    except ValueError:
        pass
    else:
        raise AssertionError("a tool was silently redefined")
    print("test_duplicate_registration_is_refused: OK")


def test_handler_exception_becomes_a_rejection():
    reg = Registry()

    def boom():
        raise RuntimeError("no such .desktop entry")

    reg.register("app.launch", ToolClass.EFFECT, {}, boom)
    res = reg.dispatch(_i("app.launch"))
    assert not res.ok and "handler failed" in res.error
    print("test_handler_exception_becomes_a_rejection: OK")


def test_describe_exposes_class_and_choices():
    reg, _ = _registry()
    by_name = {t["name"]: t for t in reg.describe()}
    assert by_name["app.launch"]["class"] == "effect"
    assert by_name["sys.tools"]["class"] == "query"
    assert by_name["app.launch"]["args"]["id"]["choices"] == list(ALLOWED_APPS)
    assert by_name["shell.swipe"]["args"]["repeat"]["required"] is False
    print("test_describe_exposes_class_and_choices: OK")


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
