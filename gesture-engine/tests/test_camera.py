#!/usr/bin/env python3
"""Host-side tests for Camera's read() predicate and stall watchdog.

No cv2/webcam dependency — when cv2 isn't importable (the host has no
OpenCV), a fake module is injected into sys.modules before camera.py is
imported, standing in for VideoCapture the same way test_injector_frames.py
fakes UInput. Verifies:
  - read() returns a pending frame immediately instead of sleeping until the
    NEXT capture (the divisor-of-camera-fps quantization bug)
  - a producer stall (read() -> False beyond reopen_after) triggers a device
    reopen and frames resume flowing afterwards
  - a reopen that itself fails (device node gone) is retried, not fatal

Run: python3 gesture-engine/tests/test_camera.py
"""
import pathlib
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    import cv2  # noqa: F401
except ImportError:
    fake = types.ModuleType("cv2")
    fake.CAP_V4L2 = 200
    fake.CAP_PROP_FOURCC = 6
    fake.CAP_PROP_FRAME_WIDTH = 3
    fake.CAP_PROP_FRAME_HEIGHT = 4
    fake.CAP_PROP_FPS = 5
    fake.VideoWriter_fourcc = lambda *a: 0
    fake.VideoCapture = None  # replaced per-test below
    sys.modules["cv2"] = fake

import cv2 as _cv2  # noqa: E402  (real or fake, both work below)

from musashi_gestures.camera import Camera  # noqa: E402


class FakeCap:
    """Scriptable VideoCapture: .feed() releases one frame to .read()."""

    instances = []

    def __init__(self, device, backend=None):
        self.opened = True
        self._gate = threading.Semaphore(0)
        self._fail = False
        self._dead = False
        FakeCap.instances.append(self)

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        return True

    def get(self, prop):
        return 0.0

    def read(self):
        if self._fail:
            time.sleep(0.01)  # a dead V4L2 read returns False, not instantly
            return False, None
        self._gate.acquire()
        if self._fail:
            return False, None
        return True, object()

    def feed(self):
        self._gate.release()

    def start_failing(self):
        self._fail = True
        self._gate.release()  # unblock a reader waiting on the gate

    def release(self):
        self.opened = False


def _make_camera(reopen_after=0.3):
    FakeCap.instances = []
    _cv2.VideoCapture = FakeCap
    return Camera(device=0, width=640, height=480, fps=30, fourcc="MJPG",
                  reopen_after=reopen_after)


def _wait_for(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_read_returns_pending_frame_immediately():
    cam = _make_camera()
    try:
        cap = FakeCap.instances[0]
        cap.feed()
        assert _wait_for(lambda: cam._frame is not None), "producer never stored the frame"
        t0 = time.monotonic()
        frame = cam.read(timeout=1.0)
        elapsed = time.monotonic() - t0
        assert frame is not None, "pending frame was not delivered"
        # The old cond.wait() slept the full timeout waiting for the NEXT
        # notify; wait_for must return as soon as it sees the pending frame.
        assert elapsed < 0.2, f"read() blocked {elapsed:.3f}s with a frame pending"
    finally:
        cap.feed()  # let the blocked producer read() return so close() joins
        cam.close()
    print("test_read_returns_pending_frame_immediately: OK")


def test_stall_triggers_reopen_and_frames_resume():
    cam = _make_camera(reopen_after=0.3)
    try:
        first = FakeCap.instances[0]
        first.feed()
        assert cam.read(timeout=1.0) is not None
        first.start_failing()
        assert _wait_for(lambda: len(FakeCap.instances) >= 2), "no reopen after stall"
        assert cam.reopens == 1
        assert not first.opened, "stalled capture was not released"
        second = FakeCap.instances[1]
        second.feed()
        assert cam.read(timeout=1.0) is not None, "no frames after reopen"
    finally:
        FakeCap.instances[-1].feed()
        cam.close()
    print("test_stall_triggers_reopen_and_frames_resume: OK")


def test_failed_reopen_is_retried():
    cam = _make_camera(reopen_after=0.2)
    try:
        first = FakeCap.instances[0]
        first.feed()
        assert cam.read(timeout=1.0) is not None

        # Device node vanishes: the next constructions fail to open, then the
        # node comes back. The watchdog must retry instead of dying.
        real_init = FakeCap.__init__
        failures = {"left": 2}

        def flaky_init(self, device, backend=None):
            real_init(self, device, backend)
            if failures["left"] > 0:
                failures["left"] -= 1
                self.opened = False

        FakeCap.__init__ = flaky_init
        try:
            first.start_failing()
            assert _wait_for(lambda: cam.reopens == 1, timeout=5.0), \
                "reopen never succeeded after transient open failures"
        finally:
            FakeCap.__init__ = real_init
        assert failures["left"] == 0, "open was not retried through the failures"
        good = FakeCap.instances[-1]
        good.feed()
        assert cam.read(timeout=1.0) is not None
    finally:
        FakeCap.instances[-1].feed()
        cam.close()
    print("test_failed_reopen_is_retried: OK")


if __name__ == "__main__":
    test_read_returns_pending_frame_immediately()
    test_stall_triggers_reopen_and_frames_resume()
    test_failed_reopen_is_retried()
    print("all camera tests passed")
