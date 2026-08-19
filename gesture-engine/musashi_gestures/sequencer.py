"""Timed touch sequences (tap, long-press, edge swipes) on a background thread.

The main loop runs at camera framerate (~15 fps, ~66 ms/frame). Long-press
(hundreds of ms of held dwell) and swipes (need ~150 ms end-to-end at a
believable velocity for phoc to recognize them as a gesture rather than a
slow drag) can't be paced by that loop without stalling frame capture. So
timed sequences run here, on a daemon thread with their own clock.

While a sequence is in flight, `busy` is true — the gesture state machine
should suppress live touch input so a real-time pinch doesn't fight with a
swipe/long-press mid-flight.
"""
import queue
import threading
import time

# (x, y) in [0, 1], swiped from just inside the edge phoc expects the
# gesture to start at, toward the middle of the screen.
_SWIPE_PATHS = {
    "up":    ((0.50, 0.995), (0.50, 0.35)),
    "down":  ((0.50, 0.005), (0.50, 0.65)),
    "left":  ((0.85, 0.50), (0.15, 0.50)),
    "right": ((0.15, 0.50), (0.85, 0.50)),
}


class TouchSequencer:
    def __init__(self, touch, tap_dwell=0.06, swipe_steps=24, swipe_step_dt=0.006,
                 swipe_down_dwell=0.02):
        self.touch = touch
        self.tap_dwell = tap_dwell
        self.swipe_steps = swipe_steps
        self.swipe_step_dt = swipe_step_dt
        self.swipe_down_dwell = swipe_down_dwell
        self._slot = 1  # slot 0 is reserved for the live pinch touch
        self._q = queue.Queue()
        self._busy = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def tap(self, x: float, y: float):
        self._q.put(("tap", x, y, self.tap_dwell))

    def long_press(self, x: float, y: float, hold: float = 0.7):
        self._q.put(("tap", x, y, hold))

    def edge_swipe(self, direction: str):
        if direction in _SWIPE_PATHS:
            self._q.put(("swipe", direction))

    def stop(self):
        self._stop.set()
        self._q.put(None)
        self._thread.join(timeout=1.0)

    # -- worker ---------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                return
            self._busy.set()
            try:
                if item[0] == "tap":
                    _, x, y, dwell = item
                    self._do_tap(x, y, dwell)
                elif item[0] == "swipe":
                    self._do_swipe(item[1])
            finally:
                self._busy.clear()

    def _do_tap(self, x: float, y: float, dwell: float):
        self.touch.down(self._slot, x, y)
        time.sleep(dwell)
        self.touch.up(self._slot)

    def _do_swipe(self, direction: str):
        (x0, y0), (x1, y1) = _SWIPE_PATHS[direction]
        self.touch.down(self._slot, x0, y0)
        time.sleep(self.swipe_down_dwell)
        for i in range(1, self.swipe_steps + 1):
            t = i / self.swipe_steps
            self.touch.move(self._slot, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            time.sleep(self.swipe_step_dt)
        self.touch.up(self._slot)
