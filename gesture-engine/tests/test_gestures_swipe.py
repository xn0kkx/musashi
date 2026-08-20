#!/usr/bin/env python3
"""Host-side tests for GestureStateMachine's swipe/palm/touch logic.

No cv2/mediapipe/evdev dependency — gestures.py only imports math/collections/
dataclasses/statistics, so this runs anywhere with plain Python 3.11+.
Landmarks are synthesized directly (no camera, no monkeypatching), unlike
test_injector_frames.py which needs a FakeUInput. Style matches that file:
plain assert + print("name: OK"), no pytest.

Run: python3 gesture-engine/tests/test_gestures_swipe.py
"""
import pathlib
import sys
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from musashi_gestures.gestures import (  # noqa: E402
    GestureStateMachine,
    WRIST, THUMB_TIP, INDEX_MCP, INDEX_PIP, INDEX_TIP,
    MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP,
    RING_PIP, RING_TIP, PINKY_PIP, PINKY_TIP,
)

_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "musashi_gestures" / "config.toml"


def _cfg(**section_overrides) -> dict:
    """Loads the real packaged config.toml, so these tests validate the
    shipped defaults rather than a parallel set of magic numbers.
    """
    cfg = tomllib.loads(_CONFIG_PATH.read_text())
    for section, values in section_overrides.items():
        cfg.setdefault(section, {}).update(values)
    return cfg


class _Seq:
    """Stand-in for TouchSequencer — the state machine only reads .busy."""
    busy = False


def _hand(cx=0.5, cy=0.5, scale=0.12, curl=(), thumb=0.75, pinch=False):
    """Synthesizes 21 (x, y, z) landmarks for one hand pose.

    Layout points the hand "up" (fingers toward smaller y). Only the indices
    gestures.py actually reads are made meaningful; everything else defaults
    to the wrist position.
      - WRIST -> MIDDLE_MCP distance is exactly `scale` (the hand-size unit).
      - Extended fingers score a tip/PIP distance ratio of ~1.41 (comfortably
        above palm_extend_ratio); fingers named in `curl` score ~0.85
        (comfortably below).
      - THUMB_TIP is placed so its distance from INDEX_MCP, divided by
        `scale`, is exactly `thumb`.
      - `pinch=True` collapses INDEX_TIP onto THUMB_TIP (pinch_index ~= 0)
        and leaves the index un-extended, as a real index+thumb pinch would.
    """
    lm = [(cx, cy, 0.0)] * 21
    lm[WRIST] = (cx, cy, 0.0)
    lm[MIDDLE_MCP] = (cx, cy - scale, 0.0)
    index_mcp = (cx - 0.35 * scale, cy - 0.95 * scale, 0.0)
    lm[INDEX_MCP] = index_mcp

    def set_finger(pip_idx, tip_idx, curled):
        pip_dist = 1.35 * scale
        tip_dist = 0.85 * pip_dist if curled else 1.9 * scale
        lm[pip_idx] = (cx, cy - pip_dist, 0.0)
        lm[tip_idx] = (cx, cy - tip_dist, 0.0)

    set_finger(INDEX_PIP, INDEX_TIP, "i" in curl)
    set_finger(MIDDLE_PIP, MIDDLE_TIP, "m" in curl)
    set_finger(RING_PIP, RING_TIP, "r" in curl)
    set_finger(PINKY_PIP, PINKY_TIP, "p" in curl)

    thumb_tip = (index_mcp[0] + thumb * scale, index_mcp[1], 0.0)
    lm[THUMB_TIP] = thumb_tip

    if pinch:
        lm[INDEX_TIP] = thumb_tip

    return lm


def _linear_hands(x0, y0, dx, dy, n=5, scale=0.12):
    """n hands tracing a straight line from (x0, y0) by (dx, dy) total."""
    return [
        _hand(cx=x0 + dx * t / (n - 1), cy=y0 + dy * t / (n - 1), scale=scale)
        for t in range(n)
    ]


def _feed(machine, hands, dt=1.0 / 15.0, t0=1000.0):
    """Feeds a sequence of hand landmarks (or None) through update()."""
    return [machine.update(h, t0 + i * dt) for i, h in enumerate(hands)]


def _swipes(acts):
    return [a.swipe for a in acts if a.swipe]


# ---------------------------------------------------------------------------

def test_swipe_fires_on_fast_palm_move():
    hands = _linear_hands(0.30, 0.50, 0.40, 0.0)  # 0.40 normalized in ~0.27s
    machine = GestureStateMachine(_cfg(), _Seq())
    acts = _feed(machine, hands)
    fired = _swipes(acts)
    assert len(fired) == 1, f"expected exactly 1 swipe, got {fired}"
    print("test_swipe_fires_on_fast_palm_move: OK")


def test_swipe_direction_respects_mirror():
    hands = _linear_hands(0.30, 0.50, 0.40, 0.0)  # rightward in raw camera coords

    m_mirrored = GestureStateMachine(_cfg(cursor={"mirror_x": True}), _Seq())
    dirs_mirrored = _swipes(_feed(m_mirrored, hands))
    assert dirs_mirrored == ["left"], dirs_mirrored

    m_plain = GestureStateMachine(_cfg(cursor={"mirror_x": False}), _Seq())
    dirs_plain = _swipes(_feed(m_plain, hands))
    assert dirs_plain == ["right"], dirs_plain
    print("test_swipe_direction_respects_mirror: OK")


def test_no_swipe_on_scale_change_only():
    # Wrist pinned in place; only hand scale changes (hand approaching the
    # camera). Delta-based velocity must not manufacture a phantom swipe.
    hands = [_hand(cx=0.5, cy=0.5, scale=s)
             for s in (0.10, 0.112, 0.124, 0.136, 0.148, 0.16)]
    machine = GestureStateMachine(_cfg(), _Seq())
    acts = _feed(machine, hands)
    assert all(a.swipe is None for a in acts), _swipes(acts)
    print("test_no_swipe_on_scale_change_only: OK")


def test_swipe_survives_dropped_palm_frames():
    cfg = _cfg(camera={"fps": 15}, cursor={"mirror_x": False})  # match the test's own frame spacing

    # (a) posture check fails for 2 frames (blurred ring/pinky) mid-flick,
    # but the wrist keeps moving — the swipe must still fire.
    hands_a = [
        _hand(cx=0.50, curl=()),
        _hand(cx=0.52, curl=()),
        _hand(cx=0.54, curl=("r", "p")),
        _hand(cx=0.90, curl=("r", "p")),
        _hand(cx=0.92, curl=()),
    ]
    m_a = GestureStateMachine(cfg, _Seq())
    fired_a = _swipes(_feed(m_a, hands_a))
    assert fired_a == ["right"], fired_a

    # (b) the hand disappears entirely (lm=None) for 2 frames mid-flick.
    hands_b = [
        _hand(cx=0.50, curl=()),
        _hand(cx=0.52, curl=()),
        None,
        None,
        _hand(cx=0.90, curl=()),
    ]
    m_b = GestureStateMachine(cfg, _Seq())
    fired_b = _swipes(_feed(m_b, hands_b))
    assert fired_b == ["right"], fired_b
    print("test_swipe_survives_dropped_palm_frames: OK")


def test_touch_released_when_palm_opens():
    machine = GestureStateMachine(_cfg(), _Seq())
    pinch_hands = [_hand(cx=0.5, cy=0.5, pinch=True) for _ in range(3)]
    acts = _feed(machine, pinch_hands)
    assert machine.touch_down is True, "touch should have engaged after 3 pinch frames"

    palm_hand = _hand(cx=0.5, cy=0.5, curl=())
    act = machine.update(palm_hand, 1000.0 + 3 * (1.0 / 15.0))
    assert act.touch is False, "opening the palm must release the live touch"
    assert machine.touch_down is False
    print("test_touch_released_when_palm_opens: OK")


def test_slow_palm_move_does_not_swipe():
    # 0.20 normalized displacement over ~1s: well under both the travel and
    # velocity gates. Guards against a future tuning pass silently making
    # every hand motion a swipe.
    hands = _linear_hands(0.40, 0.50, 0.20, 0.0, n=16)
    machine = GestureStateMachine(_cfg(), _Seq())
    acts = _feed(machine, hands, dt=1.0 / 15.0)
    assert all(a.swipe is None for a in acts), _swipes(acts)
    print("test_slow_palm_move_does_not_swipe: OK")


def test_diagonal_rejected_but_shallow_accepted():
    # Isolates the legacy axis-ratio/diagonal logic: with edge_swipe enabled
    # (the packaged default), a near-45-degree motion can arm the live-follow
    # tracker instead — it works off screen-space cursor coordinates, a
    # different projection than this test's hand-scale/aspect-corrected
    # "true diagonal", so the two don't reject at exactly the same angle.
    # That's fine (edge_swipe only ever owns up/down), but it's not what this
    # test is checking.
    cfg = _cfg(cursor={"mirror_x": False}, edge_swipe={"enabled": False})
    aspect_y = cfg["camera"]["height"] / cfg["camera"]["width"]

    # True 45 degrees *after* the aspect correction: raw dy is scaled up by
    # 1/aspect_y so the corrected dx/dy end up equal.
    dx = 0.20
    hands_diag = _linear_hands(0.40, 0.40, dx, dx / aspect_y)
    m_diag = GestureStateMachine(cfg, _Seq())
    acts_diag = _feed(m_diag, hands_diag)
    assert all(a.swipe is None for a in acts_diag), _swipes(acts_diag)
    reasons = [a.debug.get("swipe_reject") for a in acts_diag if a.debug.get("swipe_reject")]
    assert any(r.startswith("diagonal") for r in reasons), reasons

    # ~25 degrees off horizontal after correction: mostly-horizontal motion
    # must still be accepted as a left/right swipe.
    import math
    dy_eff = dx * math.tan(math.radians(25))
    hands_shallow = _linear_hands(0.40, 0.40, dx, dy_eff / aspect_y)
    m_shallow = GestureStateMachine(cfg, _Seq())
    dirs = _swipes(_feed(m_shallow, hands_shallow))
    assert dirs == ["right"], dirs
    print("test_diagonal_rejected_but_shallow_accepted: OK")


def test_open_palm_accepts_relaxed_thumb():
    m_relaxed = GestureStateMachine(_cfg(), _Seq())
    act = m_relaxed.update(_hand(thumb=0.65, curl=()), 1000.0)
    assert act.state == "palm", act.state

    m_tucked = GestureStateMachine(_cfg(), _Seq())
    act = m_tucked.update(_hand(thumb=0.55, curl=()), 1000.0)
    assert act.state != "palm", act.state
    print("test_open_palm_accepts_relaxed_thumb: OK")


def test_pinch_is_never_palm():
    machine = GestureStateMachine(_cfg(), _Seq())
    # Middle/ring/pinky extended, index pinched to the thumb — a posture a
    # naive "3 of 4 fingers" count would misclassify as an open palm.
    act = machine.update(_hand(pinch=True, curl=()), 1000.0)
    assert act.state != "palm", act.state
    print("test_pinch_is_never_palm: OK")


def test_cooldown_blocks_immediate_second_swipe():
    hands = [
        _hand(cx=0.50, curl=()),
        _hand(cx=0.52, curl=()),
        _hand(cx=0.90, curl=()),   # fires here
        _hand(cx=1.10, curl=()),  # immediate second attempt: must be blocked
    ]
    machine = GestureStateMachine(_cfg(cursor={"mirror_x": False}), _Seq())
    acts = _feed(machine, hands)
    fired = _swipes(acts)
    assert fired == ["right"], fired
    assert acts[3].debug.get("swipe_reject") == "cooldown", acts[3].debug

    fire_idx = next(i for i, a in enumerate(acts) if a.swipe)
    episode = acts[fire_idx].debug.get("episode")
    assert episode is not None, "the episode summary must close out on fire"
    assert episode["verdict"] == "fire", episode
    assert episode["max_v"] > 0.0 and episode["max_travel"] > 0.0, episode
    print("test_cooldown_blocks_immediate_second_swipe: OK")


def test_reject_reason_vocabulary():
    allowed = {"cooldown", "no-history", "short-window", "too-short", "too-slow", "diagonal"}
    seen = set()

    m = GestureStateMachine(_cfg(), _Seq())

    _, r, v, travel = m._detect_swipe(1000.0)
    seen.add(r); assert r == "no-history" and v == 0.0 and travel == 0.0

    m._motion.append((1000.0, 0.5, 0.5, 0.12))
    _, r, _, _ = m._detect_swipe(1000.05)
    seen.add(r); assert r == "no-history"

    m._motion.append((1000.03, 0.5, 0.5, 0.12))  # dt=0.03 < swipe_min_window(0.06)
    _, r, _, _ = m._detect_swipe(1000.05)
    seen.add(r); assert r == "short-window"

    m._motion.clear()
    m._motion.append((1000.0, 0.50, 0.50, 0.12))
    m._motion.append((1000.10, 0.501, 0.50, 0.12))  # tiny travel
    _, r, v, travel = m._detect_swipe(1000.10)
    seen.add(r.split(":")[0]); assert r.startswith("too-short") and v > 0 and travel > 0

    m._motion.clear()
    m._motion.append((1000.0, 0.40, 0.40, 0.12))
    m._motion.append((1000.1, 0.60, 0.60, 0.12))  # equal dx/dy -> diagonal
    _, r, v, travel = m._detect_swipe(1000.1)
    seen.add(r.split(":")[0]); assert r.startswith("diagonal") and v > 0 and travel > 0

    # "too-slow" needs travel >= swipe_min_travel with velocity still under
    # swipe_velocity, which under the packaged swipe_window (0.30s) is
    # unreachable by construction (0.8 hand-scales / 2.0 hand-scales/s =
    # 0.4s > 0.30s window) — that's intentional, see config.toml. Exercise
    # the code path directly with a wider window.
    m_wide = GestureStateMachine(_cfg(gestures={"swipe_window": 1.0}), _Seq())
    m_wide._motion.append((1000.0, 0.50, 0.50, 0.12))
    m_wide._motion.append((1000.5, 0.602, 0.50, 0.12))  # travel=0.85, v=1.7
    _, r, v, travel = m_wide._detect_swipe(1000.5)
    seen.add(r.split(":")[0]); assert r.startswith("too-slow") and v > 0 and travel > 0

    # fire, then an immediate second call is blocked by cooldown.
    m._motion.clear()
    m._last_swipe = -1e9
    m._motion.append((1000.0, 0.40, 0.40, 0.12))
    m._motion.append((1000.1, 0.90, 0.40, 0.12))
    d, r, v, travel = m._detect_swipe(1000.1)
    assert d == "right" and r == "fire" and v > 0 and travel > 0
    _, r, v, travel = m._detect_swipe(1000.15)
    seen.add(r); assert r == "cooldown" and v == 0.0 and travel == 0.0

    assert seen <= allowed, seen
    print("test_reject_reason_vocabulary: OK")


if __name__ == "__main__":
    test_swipe_fires_on_fast_palm_move()
    test_swipe_direction_respects_mirror()
    test_no_swipe_on_scale_change_only()
    test_swipe_survives_dropped_palm_frames()
    test_touch_released_when_palm_opens()
    test_slow_palm_move_does_not_swipe()
    test_diagonal_rejected_but_shallow_accepted()
    test_open_palm_accepts_relaxed_thumb()
    test_pinch_is_never_palm()
    test_cooldown_blocks_immediate_second_swipe()
    test_reject_reason_vocabulary()
    print("all tests passed")
