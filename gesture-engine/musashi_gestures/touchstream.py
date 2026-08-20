"""Resample and emit a live touch contact at a steady rate, decoupled from the
camera's ~15fps frame cadence.

phoc measures fling velocity over the last 150ms of touch events (first vs.
last event in that window — see gesture-swipe.c upstream). At 15fps that
window holds only 2-3 events: a coarse, quantized velocity estimate and a
visibly choppy drag on screen. TouchStreamer runs its own clock (same
architectural pattern as TouchSequencer's daemon thread — see sequencer.py)
and re-emits the latest known position at `rate_hz`, interpolating between
the last two camera-frame keyframes and extrapolating past the newest one
when no fresher keyframe has arrived yet. This is the same principle as the
touch resampler Android ships (frameworks/native/libs/input/InputTransport.cpp):
interpolate when a future sample exists, extrapolate when it doesn't.

One deliberate difference from Android's numbers: their resampler fills tiny
gaps between an already-high-rate source (a real touchscreen at 60-120Hz), so
an 8ms prediction ceiling makes sense there. Our source is a ~15fps camera —
consecutive keyframes are ~66ms apart, not ~8-16ms — so by the time a new
`update()` lands, `now` is already at or past that keyframe's own timestamp:
there is essentially never a "future" sample to interpolate towards, only a
"most recent" one to extrapolate from. `max_predict` here is a dead-reckoning
ceiling sized to comfortably cover one full camera frame interval (default
0.10s against an expected ~0.066s gap), not a sub-frame smoothing constant —
too small and most of each segment renders as a frozen point instead of
continuous motion (measured: a 16ms ceiling against a 66ms source produced
one interpolated point plus one clamped-extrapolation point per keyframe,
then held flat for the rest of the gap). The watchdog is the backstop for a
truly stalled source; `max_predict` only bounds a single missed/delayed frame.

This is deliberately a separate module from TouchSequencer: the sequencer
plays back a fixed, pre-recorded path (tap/long-press/legacy swipe) with no
external driver; the streamer is driven live, frame by frame, by whatever
caller owns it (the edge-drag tracker in gestures.py). Both ultimately call
the same TouchInjector, on the same reserved slot (1) — by convention, not
by locking here: whoever wires them together (gesture-engine's main loop)
must guarantee only one of the two is ever active on that slot at a time.
"""
import threading
import time


class TouchStreamer:
    """Drives one TouchInjector slot at a steady rate while a live-follow
    touch (e.g. an edge-drag) is in flight.

    `cfg` is a plain dict (the `[edge_swipe.stream]` config section) — not
    **splat, so new keys never crash the constructor the way `Camera(**cfg
    ["camera"])`/`TouchSequencer(touch, **cfg["touch"])` would.
    """

    def __init__(self, touch, cfg: dict):
        self.touch = touch
        self.slot = cfg.get("slot", 1)
        self.rate_hz = cfg.get("rate_hz", 120)
        self.mode = cfg.get("mode", "extrapolate")  # "extrapolate" | "interpolate" | "off"
        self.max_predict = cfg.get("max_predict", 0.10)
        self.min_delta = cfg.get("min_delta", 0.0005)
        self.watchdog = cfg.get("watchdog", 1.5)

        self._lock = threading.Lock()
        self._active = False
        self._k0 = None          # (t, x, y) older keyframe
        self._k1 = None          # (t, x, y) newer keyframe
        self._last_update = 0.0
        self._last_emitted = None
        self._stats = {"keyframes": 0, "moves": 0, "predicted": 0, "held": 0}

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def begin(self, x: float, y: float, t: float) -> None:
        """Press the contact down at (x, y) and start streaming from there."""
        with self._lock:
            self._active = True
            self._k0 = self._k1 = (t, x, y)
            self._last_update = t
            self._last_emitted = (x, y)
            self._stats = {"keyframes": 1, "moves": 0, "predicted": 0, "held": 0}
        self.touch.down(self.slot, x, y)

    def update(self, x: float, y: float, t: float) -> None:
        """Feed a fresh keyframe (the current hand position). Called once per
        camera frame while the drag is in flight; the background thread
        interpolates/extrapolates between calls.
        """
        with self._lock:
            if not self._active:
                return
            if (x, y) == (self._k1[1], self._k1[2]):
                return
            self._k0 = self._k1
            self._k1 = (t, x, y)
            self._last_update = t
            self._stats["keyframes"] += 1

    def end(self, t: float) -> None:
        """Lift the contact. Idempotent — a no-op if already inactive."""
        with self._lock:
            if not self._active:
                return
            self._active = False
        self.touch.up(self.slot)

    def cancel(self) -> None:
        """Same wire effect as end() (a touch up), named separately so
        callers can express *why* the stream stopped — a clean release vs.
        an aborted drag — even though phoc sees an identical event either way.
        """
        self.end(time.monotonic())

    def take_stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def stop(self) -> None:
        """Shut the background thread down. Mirrors TouchSequencer.stop()."""
        self._stop.set()
        self._thread.join(timeout=1.0)

    # -- worker ---------------------------------------------------------
    def _run(self) -> None:
        period = 1.0 / self.rate_hz
        next_tick = time.monotonic()
        while not self._stop.is_set():
            next_tick += period
            now = time.monotonic()
            sleep_for = next_tick - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_tick = now  # fell behind — resync instead of spinning to catch up

            with self._lock:
                active = self._active
                k0, k1 = self._k0, self._k1
                last_update = self._last_update
                mode = self.mode

            if not active or k1 is None:
                continue

            if (time.monotonic() - last_update) > self.watchdog:
                # Stuck source (camera loop hung, exception upstream, whatever)
                # — never leave a contact pressed on the compositor forever.
                self.end(time.monotonic())
                continue

            now = time.monotonic()
            rendered = self._render(now, k0, k1, mode)
            if rendered is None:
                continue
            x, y, predicted = rendered

            with self._lock:
                if not self._active:
                    continue
                last = self._last_emitted
                if (last is not None
                        and abs(x - last[0]) < self.min_delta
                        and abs(y - last[1]) < self.min_delta):
                    self._stats["held"] += 1
                    continue
                self._last_emitted = (x, y)
                self._stats["moves"] += 1
                if predicted:
                    self._stats["predicted"] += 1

            self.touch.move(self.slot, x, y)

    def _render(self, now: float, k0, k1, mode: str):
        """Returns (x, y, predicted) for `now`, or None if nothing to emit."""
        if mode == "off":
            return (k1[1], k1[2], False)

        t0, x0, y0 = k0
        t1, x1, y1 = k1

        if now <= t1:
            if t1 <= t0:
                return (x1, y1, False)
            frac = max(0.0, min(1.0, (now - t0) / (t1 - t0)))
            return (x0 + (x1 - x0) * frac, y0 + (y1 - y0) * frac, False)

        # now > t1: no fresher keyframe has arrived yet.
        if mode != "extrapolate":
            return (x1, y1, False)

        dt = t1 - t0
        if dt <= 0:
            return (x1, y1, False)
        predict_t = min(now - t1, self.max_predict)
        if predict_t <= 0:
            return (x1, y1, False)
        vx = (x1 - x0) / dt
        vy = (y1 - y0) / dt
        return (x1 + vx * predict_t, y1 + vy * predict_t, True)
