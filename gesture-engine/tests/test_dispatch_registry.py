#!/usr/bin/env python3
"""Regression test for the gesture engine's intent-based dispatch.

Before V0, `__main__.py` ran `touch.up(0); sequencer.edge_swipe(dir)` inline
from an `if act.swipe:`. Now the same gesture becomes an Intent that the
Registry validates and routes. This pins the *effector calls* that come out
the other side, so the inversion cannot silently change what gestures do.

`__main__` pulls in camera.py/hands.py, which need cv2 and mediapipe; those
are stubbed in sys.modules so this runs on a bare host like the other tests
here. Style: plain assert + print("name: OK"), no pytest.

Run: python3 gesture-engine/tests/test_dispatch_registry.py
"""
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

for _name in (
    "cv2",
    "mediapipe",
    "mediapipe.tasks",
    "mediapipe.tasks.python",
    "mediapipe.tasks.python.vision",
    "mediapipe.tasks.python.core",
    "mediapipe.tasks.python.core.base_options",
):
    sys.modules.setdefault(_name, types.ModuleType(_name))
sys.modules["mediapipe.tasks.python.core.base_options"].BaseOptions = object

from musashi_gestures.__main__ import _build_gesture_registry  # noqa: E402
from musashi_gestures.intents import Intent  # noqa: E402


class _Touch:
    def __init__(self):
        self.calls = []

    def up(self, slot):
        self.calls.append(("up", slot))


class _Seq:
    def __init__(self):
        self.calls = []

    def edge_swipe(self, direction):
        self.calls.append(("edge_swipe", direction))

    def long_press(self, x, y, hold):
        self.calls.append(("long_press", x, y, hold))


class _Machine:
    long_press_hold = 0.7


def _fixture():
    touch, seq = _Touch(), _Seq()
    return touch, seq, _build_gesture_registry(touch, seq, _Machine())


def test_swipe_lifts_slot0_then_sequences():
    # The old inline order — belt-and-braces touch.up(0) *before* the
    # synthesized swipe — must be preserved exactly.
    touch, seq, reg = _fixture()
    res = reg.dispatch(Intent("shell.swipe", {"dir": "up"}, source="gesture"))
    assert res.ok, res.error
    assert touch.calls == [("up", 0)]
    assert seq.calls == [("edge_swipe", "up")]
    print("test_swipe_lifts_slot0_then_sequences: OK")


def test_all_four_directions_dispatch():
    for d in ("up", "down", "left", "right"):
        touch, seq, reg = _fixture()
        assert reg.dispatch(Intent("shell.swipe", {"dir": d})).ok
        assert seq.calls == [("edge_swipe", d)]
    print("test_all_four_directions_dispatch: OK")


def test_bogus_direction_is_rejected_before_any_effector():
    touch, seq, reg = _fixture()
    res = reg.dispatch(Intent("shell.swipe", {"dir": "sideways"}))
    assert not res.ok
    assert touch.calls == [] and seq.calls == []
    print("test_bogus_direction_is_rejected_before_any_effector: OK")


def test_long_press_passes_the_machine_hold():
    touch, seq, reg = _fixture()
    assert reg.dispatch(Intent("ui.long_press", {"x": 0.25, "y": 0.75})).ok
    assert seq.calls == [("long_press", 0.25, 0.75, 0.7)]
    assert touch.calls == []
    print("test_long_press_passes_the_machine_hold: OK")


def test_long_press_coordinates_are_not_range_clipped():
    # MediaPipe landmarks stray slightly outside [0,1] and injector._clamp
    # already handles that; the registry must not start dropping those.
    touch, seq, reg = _fixture()
    assert reg.dispatch(Intent("ui.long_press", {"x": -0.01, "y": 1.02})).ok
    assert seq.calls == [("long_press", -0.01, 1.02, 0.7)]
    print("test_long_press_coordinates_are_not_range_clipped: OK")


def test_engine_registry_exposes_only_gesture_tools():
    _, _, reg = _fixture()
    assert reg.names() == ["shell.swipe", "ui.long_press"]
    print("test_engine_registry_exposes_only_gesture_tools: OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
