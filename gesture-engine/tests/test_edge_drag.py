#!/usr/bin/env python3
"""Host-side tests for EdgeDragTracker — the live-follow edge-swipe state
machine (idle -> armed -> dragging -> cooldown). No cv2/mediapipe/evdev
dependency, plain (x, y) tuples in screen-normalized [0,1] coordinates
(the same space act.cursor already uses). Style matches test_gestures_swipe.py:
plain assert + print("name: OK"), no pytest.

Numbers below are the phoc 0.46.0 thresholds calibrated empirically on the
VM (logical 960x540, scale=2 — see ROADMAP.md): the pure-distance drag path
only accepts a touch-down within ~3-8px of the true edge (0.0037-0.0074
normalized); fling accepts from anywhere given enough velocity. The default
`edge_inset = 0.006` (~3.2px) sits inside that window on purpose.

Run: python3 gesture-engine/tests/test_edge_drag.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from musashi_gestures.edgedrag import EdgeDragTracker, EdgePhase  # noqa: E402

_DEFAULT_CFG = {
    "enabled": True,
    "directions": ["up", "down"],
    "arm_frames": 2,
    "arm_window": 0.40,
    "start_slop": 0.06,
    "axis_margin": 0.03,
    "follow_gain": 1.0,
    "axis_lock": True,
    "edge_inset": 0.006,
    "x_anchor_min": 0.20,
    "x_anchor_max": 0.80,
    "release_grace": 0.13,
    "lost_grace": 0.13,
    "max_duration": 2.5,
    "cooldown": 0.30,
}


def _cfg(**overrides):
    cfg = dict(_DEFAULT_CFG)
    cfg.update(overrides)
    return cfg


def _arm(tracker, xy=(0.5, 0.50), t0=1000.0, dt=1.0 / 15.0, frames=None):
    """Feeds enough (palm_ok, hold steady) frames to reach ARMED, returns the
    time of the last fed frame.
    """
    n = frames if frames is not None else tracker.arm_frames
    t = t0
    ev = None
    for i in range(n):
        t = t0 + i * dt
        ev = tracker.update(xy, True, True, t)
    assert tracker.phase is EdgePhase.ARMED, tracker.phase
    return t


def _sweep(tracker, x, y0, y1, t0, n=10, dt=1.0 / 15.0):
    """Feeds a vertical sweep from y0 to y1 at fixed x, returns the list of
    events (including Nones) and the final time.
    """
    events = []
    t = t0
    for i in range(1, n + 1):
        t = t0 + i * dt
        y = y0 + (y1 - y0) * i / n
        events.append(tracker.update((x, y), True, True, t))
    return events, t


def test_down_anchors_inside_home_bar():
    tracker = EdgeDragTracker(_cfg())
    t0 = _arm(tracker, xy=(0.5, 0.50))
    events, _ = _sweep(tracker, 0.5, 0.50, 0.20, t0, n=5)
    down = next(e for e in events if e is not None and e.phase == "down")
    assert down.dir == "up", down
    # 15px logical on a 540px-tall logical screen == 0.0278; the pure-distance
    # path was measured to need much tighter than that (~3-8px) — well within.
    assert down.y >= 1 - 15 / 540, f"y={down.y} not inside the home bar handle"
    print("test_down_anchors_inside_home_bar: OK")


def test_down_anchors_inside_top_bar():
    tracker = EdgeDragTracker(_cfg())
    t0 = _arm(tracker, xy=(0.5, 0.50))
    events, _ = _sweep(tracker, 0.5, 0.50, 0.80, t0, n=5)
    down = next(e for e in events if e is not None and e.phase == "down")
    assert down.dir == "down", down
    assert down.y <= 32 / 540, f"y={down.y} not inside the top bar handle"
    print("test_down_anchors_inside_top_bar: OK")


def test_axis_lock_keeps_lateral_under_reject_threshold():
    tracker = EdgeDragTracker(_cfg())
    t0 = _arm(tracker, xy=(0.5, 0.50))
    # Wobble x by +/-0.05 (== 48px logical, well above the 24px phoc reject
    # threshold) while sweeping y — axis_lock must keep the emitted x pinned.
    events = []
    t = t0
    for i in range(1, 11):
        t = t0 + i * (1.0 / 15.0)
        wobble = 0.05 if i % 2 == 0 else -0.05
        y = 0.50 - 0.30 * i / 10
        events.append(tracker.update((0.5 + wobble, y), True, True, t))
    xs = [e.x for e in events if e is not None]
    assert xs, "expected at least one emitted event"
    assert max(xs) - min(xs) == 0, f"axis_lock leaked lateral movement: {xs}"
    reject_px = 24 / 960  # logical screen width
    assert (max(xs) - min(xs)) < reject_px
    print("test_axis_lock_keeps_lateral_under_reject_threshold: OK")


def test_first_move_exceeds_accept_threshold():
    tracker = EdgeDragTracker(_cfg())
    t0 = _arm(tracker, xy=(0.5, 0.50))
    events, _ = _sweep(tracker, 0.5, 0.50, 0.10, t0, n=6)
    down = next(e for e in events if e is not None and e.phase == "down")
    assert down.y == 1 - tracker.edge_inset, "down must land exactly on the edge"
    first_move = next(e for e in events if e is not None and e.phase == "move")
    accept_px = 16 / 540  # logical screen height
    # origin_mode="arm": the delta is measured from the ARMED position, so
    # the first MOVE already carries the whole start_slop's worth of travel
    # in a single jump from the DOWN position — no dead zone.
    delta = abs(first_move.y - down.y)
    assert delta >= accept_px, f"first move only moved {delta}, need >= {accept_px}"
    print("test_first_move_exceeds_accept_threshold: OK")


def test_full_sweep_exceeds_30_percent():
    tracker = EdgeDragTracker(_cfg())
    t0 = _arm(tracker, xy=(0.5, 0.50))
    # A 0.20 hand-frame sweep maps 1:1 (follow_gain=1.0) to screen units here.
    events, _ = _sweep(tracker, 0.5, 0.50, 0.20, t0, n=10)
    moves = [e for e in events if e is not None]
    last = moves[-1]
    total_travel = abs((1 - tracker.edge_inset) - last.y)
    assert total_travel >= 0.30, f"travel={total_travel}, need >= 0.30"
    print("test_full_sweep_exceeds_30_percent: OK")


def test_release_on_palm_loss_emits_exactly_one_up():
    tracker = EdgeDragTracker(_cfg(release_grace=0.05))
    t0 = _arm(tracker, xy=(0.5, 0.50))
    events, t = _sweep(tracker, 0.5, 0.50, 0.20, t0, n=5)
    assert tracker.active

    ups = []
    t_loss = t
    for i in range(1, 10):
        t_loss = t + i * (1.0 / 15.0)
        ev = tracker.update((0.5, 0.30), False, False, t_loss)  # palm closes
        if ev is not None:
            ups.append(ev)
    assert len(ups) == 1, ups
    assert ups[0].phase == "up", ups[0]
    assert not tracker.active
    print("test_release_on_palm_loss_emits_exactly_one_up: OK")


def test_lost_hand_grace_then_up():
    tracker = EdgeDragTracker(_cfg(lost_grace=0.05))
    t0 = _arm(tracker, xy=(0.5, 0.50))
    events, t = _sweep(tracker, 0.5, 0.50, 0.20, t0, n=5)
    assert tracker.active

    ev = tracker.update(None, False, False, t + 0.02)
    assert ev is None, "loss inside the grace window must not release yet"
    assert tracker.active

    ev = tracker.update(None, False, False, t + 0.10)
    assert ev is not None and ev.phase == "up", ev
    assert not tracker.active
    print("test_lost_hand_grace_then_up: OK")


def test_horizontal_dominant_does_not_start_drag():
    tracker = EdgeDragTracker(_cfg())
    t0 = _arm(tracker, xy=(0.5, 0.50))
    events = []
    t = t0
    for i in range(1, 8):
        t = t0 + i * (1.0 / 15.0)
        x = 0.50 + 0.30 * i / 7
        events.append(tracker.update((x, 0.50), True, True, t))
    assert all(e is None for e in events), events
    assert tracker.phase is EdgePhase.IDLE, tracker.phase
    print("test_horizontal_dominant_does_not_start_drag: OK")


def test_cooldown_prevents_immediate_rearm():
    tracker = EdgeDragTracker(_cfg(cooldown=0.30))
    t0 = _arm(tracker, xy=(0.5, 0.50))
    events, t = _sweep(tracker, 0.5, 0.50, 0.20, t0, n=5)
    ev = tracker.update((0.5, 0.20), False, False, t + 0.20)  # release
    assert ev is not None and ev.phase == "up"
    assert tracker.phase is EdgePhase.COOLDOWN

    # Try to arm again immediately — must stay blocked.
    t2 = t + 0.20
    for i in range(1, 5):
        t2 += 1.0 / 15.0
        tracker.update((0.5, 0.50), True, True, t2)
    assert tracker.phase in (EdgePhase.COOLDOWN, EdgePhase.IDLE), tracker.phase
    assert not tracker.active
    print("test_cooldown_prevents_immediate_rearm: OK")


def test_max_duration_emits_cancel():
    tracker = EdgeDragTracker(_cfg(max_duration=0.15))
    t0 = _arm(tracker, xy=(0.5, 0.50))
    events, t = _sweep(tracker, 0.5, 0.50, 0.10, t0, n=3)
    assert tracker.active
    ev = tracker.update((0.5, 0.45), True, True, t + 0.5)
    assert ev is not None and ev.phase == "cancel", ev
    assert not tracker.active
    print("test_max_duration_emits_cancel: OK")


def test_abort_releases_in_flight_drag():
    tracker = EdgeDragTracker(_cfg())
    t0 = _arm(tracker, xy=(0.5, 0.50))
    _sweep(tracker, 0.5, 0.50, 0.20, t0, n=5)
    assert tracker.active
    ev = tracker.abort(t0 + 10.0)
    assert ev is not None and ev.phase == "cancel"
    assert not tracker.active
    assert tracker.phase is EdgePhase.COOLDOWN

    # abort() on an idle tracker is a harmless no-op.
    tracker2 = EdgeDragTracker(_cfg())
    assert tracker2.abort(1000.0) is None
    print("test_abort_releases_in_flight_drag: OK")


def test_episode_summary_after_release():
    tracker = EdgeDragTracker(_cfg())
    assert tracker.take_episode() is None
    t0 = _arm(tracker, xy=(0.5, 0.50))
    events, t = _sweep(tracker, 0.5, 0.50, 0.20, t0, n=5)
    tracker.update((0.5, 0.20), False, False, t + 0.20)
    summary = tracker.take_episode()
    assert summary is not None and summary["frames"] > 0, summary
    assert tracker.take_episode() is None, "episode summary must be one-shot"
    print("test_episode_summary_after_release: OK")


if __name__ == "__main__":
    test_down_anchors_inside_home_bar()
    test_down_anchors_inside_top_bar()
    test_axis_lock_keeps_lateral_under_reject_threshold()
    test_first_move_exceeds_accept_threshold()
    test_full_sweep_exceeds_30_percent()
    test_release_on_palm_loss_emits_exactly_one_up()
    test_lost_hand_grace_then_up()
    test_horizontal_dominant_does_not_start_drag()
    test_cooldown_prevents_immediate_rearm()
    test_max_duration_emits_cancel()
    test_abort_releases_in_flight_drag()
    test_episode_summary_after_release()
    print("all tests passed")
