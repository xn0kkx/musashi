#!/usr/bin/env python3
"""Host-side tests for TouchStreamer's resampling/emission thread.

No evdev dependency — a lightweight fake stands in for TouchInjector, since
TouchStreamer only ever calls .down(slot,x,y)/.move(slot,x,y)/.up(slot) on
whatever it's given. Real threading + wall-clock sleeps, same pattern as
test_injector_frames.py's test_edge_swipe_up. Style: plain assert +
print("name: OK"), no pytest.

Run: python3 gesture-engine/tests/test_touch_stream.py
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from musashi_gestures.touchstream import TouchStreamer  # noqa: E402


class _FakeTouch:
    def __init__(self):
        self.downs = []
        self.moves = []
        self.ups = []

    def down(self, slot, x, y):
        self.downs.append((slot, x, y))

    def move(self, slot, x, y):
        self.moves.append((slot, x, y))

    def up(self, slot):
        self.ups.append(slot)


def _drive_camera_frames(streamer, start_xy, deltas, frame_dt=1.0 / 15.0):
    """Feeds `begin` then a sequence of `update`s spaced by real sleeps of
    `frame_dt`, simulating a ~15fps camera loop. Returns the wall-clock
    duration actually elapsed.
    """
    t0 = time.monotonic()
    x, y = start_xy
    streamer.begin(x, y, t0)
    for dx, dy in deltas:
        time.sleep(frame_dt)
        x, y = x + dx, y + dy
        streamer.update(x, y, time.monotonic())
    return time.monotonic() - t0


def test_emits_more_moves_than_keyframes():
    touch = _FakeTouch()
    streamer = TouchStreamer(touch, {"rate_hz": 120, "mode": "extrapolate"})
    try:
        # 5 camera keyframes over ~330ms (~15fps) moving steadily upward.
        _drive_camera_frames(streamer, (0.5, 0.99), [(0.0, -0.05)] * 5)
        time.sleep(0.05)
        streamer.end(time.monotonic())
        time.sleep(0.05)  # let the worker notice `end` and stop emitting
        assert len(touch.moves) >= 20, f"expected >=20 moves, got {len(touch.moves)}"
        print(f"test_emits_more_moves_than_keyframes: OK ({len(touch.moves)} moves for 5 keyframes)")
    finally:
        streamer.stop()


def test_monotonic_y_for_up_drag():
    touch = _FakeTouch()
    streamer = TouchStreamer(touch, {"rate_hz": 120, "mode": "extrapolate"})
    try:
        _drive_camera_frames(streamer, (0.5, 0.99), [(0.0, -0.06)] * 5)
        streamer.end(time.monotonic())
        time.sleep(0.05)
        ys = [y for (_slot, _x, y) in touch.moves]
        assert len(ys) > 5, ys
        assert ys == sorted(ys, reverse=True), f"y must monotonically decrease: {ys}"
        print("test_monotonic_y_for_up_drag: OK")
    finally:
        streamer.stop()


def test_never_emits_after_end():
    touch = _FakeTouch()
    streamer = TouchStreamer(touch, {"rate_hz": 120, "mode": "extrapolate"})
    try:
        _drive_camera_frames(streamer, (0.5, 0.99), [(0.0, -0.05)] * 3)
        streamer.end(time.monotonic())
        count_at_end = len(touch.moves)
        time.sleep(0.1)  # >> one rate period; nothing should be emitted
        assert len(touch.moves) == count_at_end, (
            f"moves grew after end(): {count_at_end} -> {len(touch.moves)}"
        )
        assert touch.ups == [1]
        print("test_never_emits_after_end: OK")
    finally:
        streamer.stop()


def test_holds_position_when_source_stalls():
    touch = _FakeTouch()
    streamer = TouchStreamer(
        touch, {"rate_hz": 120, "mode": "extrapolate", "max_predict": 0.016, "watchdog": 5.0}
    )
    try:
        t0 = time.monotonic()
        streamer.begin(0.5, 0.99, t0)
        time.sleep(1.0 / 15.0)
        streamer.update(0.5, 0.90, time.monotonic())
        last_keyframe_y = 0.90
        # No further updates for a while — well past max_predict, well under
        # the watchdog. Extrapolation must clamp, not run away.
        time.sleep(0.3)
        streamer.end(time.monotonic())
        time.sleep(0.05)
        ys = [y for (_slot, _x, y) in touch.moves]
        assert ys, "expected at least one emitted move"
        # Extrapolation is capped at max_predict past the last keyframe's own
        # velocity — bound how far any single emitted point can have drifted
        # from the last real keyframe (generous multiple, not an exact model).
        max_drift = max(abs(y - last_keyframe_y) for y in ys)
        assert max_drift < 0.2, f"extrapolation drifted too far: {max_drift}"
        print("test_holds_position_when_source_stalls: OK")
    finally:
        streamer.stop()


def test_watchdog_releases_stuck_contact():
    touch = _FakeTouch()
    streamer = TouchStreamer(touch, {"rate_hz": 120, "mode": "extrapolate", "watchdog": 0.15})
    try:
        streamer.begin(0.5, 0.99, time.monotonic())
        streamer.update(0.5, 0.90, time.monotonic())
        assert streamer.active
        time.sleep(0.35)  # > watchdog
        assert not streamer.active, "watchdog should have released the contact"
        assert touch.ups == [1], touch.ups
        print("test_watchdog_releases_stuck_contact: OK")
    finally:
        streamer.stop()


def test_begin_resets_stats():
    touch = _FakeTouch()
    streamer = TouchStreamer(touch, {"rate_hz": 120})
    try:
        _drive_camera_frames(streamer, (0.5, 0.99), [(0.0, -0.05)] * 3)
        streamer.end(time.monotonic())
        time.sleep(0.02)
        stats1 = streamer.take_stats()
        assert stats1["keyframes"] == 4, stats1  # begin + 3 updates
        assert stats1["moves"] > 0, stats1

        _drive_camera_frames(streamer, (0.5, 0.99), [(0.0, -0.05)] * 2)
        stats2 = streamer.take_stats()
        assert stats2["keyframes"] == 3, stats2  # reset by the second begin()
        streamer.end(time.monotonic())
        print("test_begin_resets_stats: OK")
    finally:
        streamer.stop()


if __name__ == "__main__":
    test_emits_more_moves_than_keyframes()
    test_monotonic_y_for_up_drag()
    test_never_emits_after_end()
    test_holds_position_when_source_stalls()
    test_watchdog_releases_stuck_contact()
    test_begin_resets_stats()
    print("all tests passed")
