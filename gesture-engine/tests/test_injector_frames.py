#!/usr/bin/env python3
"""Protocol-level test for TouchInjector/PointerInjector, no /dev/uinput needed.

Monkeypatches evdev.UInput with a fake that just logs write()/syn() calls, so
this runs on the host without root and without touching real device nodes —
safe to run anywhere, including outside the VM. Verifies:
  - down() emits SLOT -> TRACKING_ID(>=0) -> POSITION_X -> POSITION_Y ->
    BTN_TOUCH(1), all inside a single SYN_REPORT frame
  - up() emits TRACKING_ID(-1) + BTN_TOUCH(0) in one frame
  - a second concurrent contact doesn't re-send BTN_TOUCH(1)
  - TouchSequencer.edge_swipe("up") starts near the bottom edge and ends
    higher up, with monotonic interpolation steps

Run: python3 gesture-engine/tests/test_injector_frames.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import evdev
from evdev import ecodes as e


class FakeUInput:
    """Records (type, code, value) writes and frame boundaries (syn())."""

    def __init__(self, caps, **kwargs):
        self.caps = caps
        self.kwargs = kwargs
        self.log = []       # flat list of (type, code, value)
        self.frames = []    # list of frames, each a list of (type, code, value)
        self._cur = []

    def write(self, etype, code, value):
        entry = (etype, code, value)
        self.log.append(entry)
        self._cur.append(entry)

    def syn(self):
        self.frames.append(self._cur)
        self._cur = []

    def close(self):
        pass


def patched(fn):
    def wrapper(*args, **kwargs):
        real_uinput = evdev.UInput
        evdev.UInput = FakeUInput
        try:
            import importlib
            import musashi_gestures.injector as injector_mod
            importlib.reload(injector_mod)
            injector_mod.UInput = FakeUInput
            return fn(injector_mod, *args, **kwargs)
        finally:
            evdev.UInput = real_uinput
    return wrapper


@patched
def test_touch_down_frame(injector_mod):
    t = injector_mod.TouchInjector()
    t.down(0, 0.5, 0.5)
    frames = t.ui.frames
    assert len(frames) == 1, f"expected 1 SYN frame, got {len(frames)}"
    codes = [c for (_typ, c, _v) in frames[0]]
    assert codes.index(e.ABS_MT_SLOT) < codes.index(e.ABS_MT_TRACKING_ID) < \
           codes.index(e.ABS_MT_POSITION_X) < codes.index(e.ABS_MT_POSITION_Y) < \
           codes.index(e.BTN_TOUCH), f"bad ordering: {codes}"
    tracking_id = next(v for (typ, c, v) in frames[0] if c == e.ABS_MT_TRACKING_ID)
    assert tracking_id >= 0, "tracking id must be >= 0 on down"
    touch_val = next(v for (typ, c, v) in frames[0] if c == e.BTN_TOUCH)
    assert touch_val == 1, "BTN_TOUCH must be 1 on first contact"
    print("test_touch_down_frame: OK")


@patched
def test_touch_up_frame(injector_mod):
    t = injector_mod.TouchInjector()
    t.down(0, 0.5, 0.5)
    t.up(0)
    frames = t.ui.frames
    assert len(frames) == 2
    up_frame = frames[1]
    codes = {c: v for (_typ, c, v) in up_frame}
    assert codes[e.ABS_MT_TRACKING_ID] == -1
    assert codes[e.BTN_TOUCH] == 0
    print("test_touch_up_frame: OK")


@patched
def test_second_contact_no_extra_btn_touch(injector_mod):
    t = injector_mod.TouchInjector()
    t.down(0, 0.2, 0.2)
    t.down(1, 0.8, 0.8)
    second_frame = t.ui.frames[1]
    codes = [c for (_typ, c, _v) in second_frame]
    assert e.BTN_TOUCH not in codes, "BTN_TOUCH should only fire on the first contact"
    print("test_second_contact_no_extra_btn_touch: OK")


@patched
def test_edge_swipe_up(injector_mod):
    from musashi_gestures.sequencer import TouchSequencer, _SWIPE_PATHS
    t = injector_mod.TouchInjector()
    seq = TouchSequencer(t, swipe_steps=6, swipe_step_dt=0.001)
    seq.edge_swipe("up")
    seq.stop()  # drains the queue and joins

    (x0, y0), (x1, y1) = _SWIPE_PATHS["up"]
    assert y0 > 0.9, "swipe up must start near the bottom edge"
    assert y1 < y0, "swipe up must move toward the top"

    ys = []
    for frame in t.ui.frames:
        codes = {c: v for (_typ, c, v) in frame}
        if e.ABS_MT_POSITION_Y in codes:
            ys.append(codes[e.ABS_MT_POSITION_Y])
    assert len(ys) >= 6, f"expected multiple interpolation steps, got {len(ys)}"
    assert ys == sorted(ys, reverse=True), f"swipe-up Y should monotonically decrease: {ys}"
    print("test_edge_swipe_up: OK")


if __name__ == "__main__":
    test_touch_down_frame()
    test_touch_up_frame()
    test_second_contact_no_extra_btn_touch()
    test_edge_swipe_up()
    print("all tests passed")
