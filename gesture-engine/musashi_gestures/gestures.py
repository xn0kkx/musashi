"""Gesture state machine: landmark geometry -> touch actions.

Gestures (single hand):
  point (any posture)      aim only, drives the visible pointer cursor
  index+thumb pinch        live touch: down on engage, moves with the pinch,
                            up on release (drag == held pinch + move)
  middle+thumb pinch       long-press (secondary action / context menu),
                            only when not already touching
  open palm + fast move    edge swipe (home / notifications / app pages),
                            dispatched to the TouchSequencer

All distances are normalized by hand size (wrist -> middle MCP) so gestures
work at any distance from the camera. Hysteresis (engage/release thresholds +
minimum frame count) suppresses jitter and phantom touches.

Swipe detection keeps a short history of wrist motion on every frame that has
a hand at all (not just frames where the palm posture is currently
recognized), because the fastest part of a flick is exactly when motion blur
makes MediaPipe's finger-extension landmarks least reliable. A "palm grace"
window keeps the swipe armed for a few frames after the posture check stops
passing, so a dropped frame or two mid-flick doesn't kill the gesture.
"""
import math
from collections import deque
from dataclasses import dataclass, field
from statistics import median

from .edgedrag import EdgeDragTracker, EdgePhase

WRIST, THUMB_TIP, INDEX_MCP, INDEX_PIP, INDEX_TIP = 0, 4, 5, 6, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP = 9, 10, 12
RING_PIP, RING_TIP, PINKY_PIP, PINKY_TIP = 14, 16, 18, 20

# Ranking of swipe-rejection reasons from "closest to firing" to "furthest",
# used to pick the single most useful reason to report for a whole palm
# episode (see GestureStateMachine._episode_verdict).
_REASON_RANK = {
    "diagonal": 0,
    "too-slow": 1,
    "too-short": 2,
    "short-window": 3,
    "no-history": 4,
    "cooldown": 5,
}


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class Actions:
    """What the engine should do this frame."""
    cursor: tuple | None = None          # (x, y) normalized [0,1] — aim only
    touch: bool | None = None            # live touch press/release transition
    touch_at: tuple | None = None        # live touch position when touch is down
    long_press: bool = False             # fire a long-press sequence at touch_at
    swipe: str | None = None             # "left" | "right" (up/down: see edge_drag)
    edge_drag: object | None = None      # EdgeDragEvent | None — live-follow up/down
    state: str = "idle"                  # for the overlay/debug
    debug: dict = field(default_factory=dict)


class GestureStateMachine:
    def __init__(self, cfg: dict, sequencer=None):
        g = cfg["gestures"]
        self.engage = g["pinch_engage"]
        self.release = g["pinch_release"]
        self.engage_frames = g["engage_frames"]
        self.thumb_out = g["thumb_out"]
        self.swipe_velocity = g["swipe_velocity"]
        self.swipe_cooldown = g["swipe_cooldown"]
        self.long_press_hold = g["long_press_hold"]
        self.long_press_cooldown = g["long_press_cooldown"]

        self.palm_extend_ratio = g["palm_extend_ratio"]
        self.palm_min_fingers = g["palm_min_fingers"]
        self.palm_grace_frames = g["palm_grace_frames"]
        self.swipe_arm_frames = g["swipe_arm_frames"]
        self.swipe_window = g["swipe_window"]
        self.swipe_min_window = g["swipe_min_window"]
        self.swipe_min_travel = g["swipe_min_travel"]
        self.swipe_axis_ratio = g["swipe_axis_ratio"]

        self.cursor_landmark = cfg["cursor"]["landmark"]
        r = cfg["cursor"]["active_region"]
        self.region = (r["x0"], r["y0"], r["x1"], r["y1"])
        self.mirror_x = cfg["cursor"]["mirror_x"]

        # Normalized x/y are each 0..1 over different pixel counts (camera is
        # e.g. 640x480), so identical physical motion produces a larger delta
        # in y than in x. Scale y into x-equivalent units before comparing
        # axes, so the axis-dominance test isn't silently biased toward
        # vertical. Deliberately NOT applied to the hand-size `scale` used
        # for pinch/thumb thresholds — those are calibrated against the
        # distorted metric already.
        cam = cfg.get("camera", {})
        width, height = cam.get("width"), cam.get("height")
        self._aspect_y = (height / width) if width and height else 1.0

        self.sequencer = sequencer
        # Live-follow edge-drag for up/down (see edgedrag.py). Defaults to
        # disabled if the section is absent, so a config predating this
        # feature behaves exactly as before — the legacy classifier below
        # keeps handling every direction in that case.
        self.edge_tracker = EdgeDragTracker(cfg.get("edge_swipe", {"enabled": False}))

        self.touch_down = False
        self._touch_streak = 0
        self._long_press_streak = 0
        self._last_long_press = 0.0

        # nominal frame time, used only to convert palm_grace_frames into a
        # wall-clock grace window. The grace check itself is time-based so a
        # variable camera framerate can't silently change its meaning.
        fps = cam.get("fps") or 15
        self._frame_dt = 1.0 / fps
        self._palm_grace_s = self.palm_grace_frames * self._frame_dt

        self._motion = deque(maxlen=12)   # (t, x, y, scale), unconditional
        self._palm_streak = 0
        self._last_palm_t = -1e9
        self._last_swipe = -1e9

        # Palm-episode tracking, for the one-line-per-attempt log summary.
        self._episode_open = False
        self._episode_frames = 0
        self._episode_dropped = 0
        self._episode_best = None   # (v, travel, axis_ratio)
        self._episode_reasons = []  # reasons seen this episode

    # -- posture helpers ----------------------------------------------------
    def _extended(self, lm, tip: int, pip: int) -> bool:
        """Finger is extended if its tip is farther from the wrist than its PIP."""
        return _dist(lm[tip], lm[WRIST]) > _dist(lm[pip], lm[WRIST]) * self.palm_extend_ratio

    def _open_palm(self, lm, scale: float) -> tuple:
        """Returns (is_open_palm, debug dict) so callers can log which
        sub-gate failed. Index and middle extension are mandatory (a curled
        index scores ~0.77, well under any sane ratio) so this never
        misclassifies a pinch — regardless of how loose palm_min_fingers is —
        as an open palm.
        """
        ext = {
            "i": self._extended(lm, INDEX_TIP, INDEX_PIP),
            "m": self._extended(lm, MIDDLE_TIP, MIDDLE_PIP),
            "r": self._extended(lm, RING_TIP, RING_PIP),
            "p": self._extended(lm, PINKY_TIP, PINKY_PIP),
        }
        thumb = _dist(lm[THUMB_TIP], lm[INDEX_MCP]) / scale
        ok = (
            ext["i"] and ext["m"]
            and sum(ext.values()) >= self.palm_min_fingers
            and thumb > self.thumb_out
        )
        return ok, {"ext": ext, "thumb": round(thumb, 2)}

    def _palm_hold(self, lm) -> bool:
        """Relaxed *maintenance* gate for an edge-drag already in flight.

        `_open_palm` is the strict *entry* gate (full posture + thumb-out,
        so a pinch can never be misread as an open palm). Once a drag is
        actually following the hand, that full posture shouldn't be required
        every single frame — a ring/pinky flicker or the thumb drifting
        mid-drag is exactly the kind of landmark noise a slow, deliberate
        drag will produce, and aborting on it would defeat the whole point
        of live-follow. Only index-or-middle extended is required to keep
        holding the contact down.
        """
        return (self._extended(lm, INDEX_TIP, INDEX_PIP)
                or self._extended(lm, MIDDLE_TIP, MIDDLE_PIP))

    def _map_cursor(self, lm) -> tuple:
        x, y = lm[self.cursor_landmark][0], lm[self.cursor_landmark][1]
        if self.mirror_x:
            x = 1.0 - x
        x0, y0, x1, y1 = self.region
        return (
            max(0.0, min(1.0, (x - x0) / (x1 - x0))),
            max(0.0, min(1.0, (y - y0) / (y1 - y0))),
        )

    # -- swipe detection ------------------------------------------------
    def _record_motion(self, lm, scale: float, now: float):
        """Unconditionally sample wrist motion, regardless of hand posture.
        The fastest part of a flick is exactly when the palm-posture check
        is least reliable, so motion tracking must not depend on it.
        """
        x = lm[WRIST][0]
        if self.mirror_x:
            x = 1.0 - x
        y = lm[WRIST][1] * self._aspect_y
        self._motion.append((now, x, y, scale))

    def _detect_swipe(self, now: float):
        """Returns (direction | None, reason, v, travel). `reason` is always
        populated, even on success ("fire"). `v`/`travel` are the measured
        values of the best candidate pair (0.0 if none was found at all) —
        kept as plain numbers, not just embedded in the reason string, so
        callers can track episode maxima without re-parsing text.
        """
        if now - self._last_swipe < self.swipe_cooldown:
            return None, "cooldown", 0.0, 0.0

        win = [s for s in self._motion if now - s[0] <= self.swipe_window]
        if len(win) < 2:
            return None, "no-history", 0.0, 0.0

        s_med = median(s[3] for s in win) or 1e-6

        # Prefer the highest-velocity pair — this is what lets a fast flick
        # surrounded by a slower lead-in/settle still clear the threshold.
        # Tie-break (rounded, since near-uniform real motion can produce
        # near-identical velocities across many window sizes) on travel: the
        # wider window is more robust evidence of an intentional gesture,
        # and without this a perfectly steady motion would otherwise always
        # resolve to the single smallest — and therefore shortest-travel —
        # interval, spuriously failing the travel gate.
        best = None       # (v, dx, dy, dt)
        best_key = None   # (rounded v, travel)
        for i in range(len(win)):
            for j in range(i + 1, len(win)):
                dt = win[j][0] - win[i][0]
                if dt < self.swipe_min_window:
                    continue
                dx = (win[j][1] - win[i][1]) / s_med
                dy = (win[j][2] - win[i][2]) / s_med
                v = math.hypot(dx, dy) / dt
                key = (round(v, 3), math.hypot(dx, dy))
                if best is None or key > best_key:
                    best = (v, dx, dy, dt)
                    best_key = key

        if best is None:
            return None, "short-window", 0.0, 0.0

        v, dx, dy, dt = best
        travel = math.hypot(dx, dy)

        if travel < self.swipe_min_travel:
            return None, f"too-short:{travel:.2f}", v, travel
        if v < self.swipe_velocity:
            return None, f"too-slow:{v:.2f}", v, travel

        if abs(dx) > abs(dy) * self.swipe_axis_ratio:
            direction = "right" if dx > 0 else "left"
        elif abs(dy) > abs(dx) * self.swipe_axis_ratio:
            direction = "down" if dy > 0 else "up"
        else:
            ratio = abs(dx) / max(abs(dy), 1e-6)
            return None, f"diagonal:{ratio:.2f}", v, travel

        self._last_swipe = now
        self._motion.clear()
        self._palm_streak = 0
        return direction, "fire", v, travel

    def _release_touch(self, act: Actions):
        """Force-release the live touch contact. Called from every path that
        can hand off to the sequencer, so a real-time pinch can never leave
        slot 0 pressed while a synthesized swipe/long-press uses slot 1 —
        which would make phoc see two simultaneous contacts instead of a
        clean single-finger edge gesture.
        """
        if self.touch_down:
            self.touch_down = False
            act.touch = False

    # -- episode tracking (for the one-line-per-swipe-attempt log) ---------
    def _episode_update(self, reason: str, v: float, travel: float):
        self._episode_open = True
        self._episode_frames += 1
        self._episode_reasons.append(reason)
        if self._episode_best is None or v > self._episode_best[0]:
            self._episode_best = (v, travel)

    def _episode_close(self) -> dict | None:
        if not self._episode_open:
            return None
        if "fire" in self._episode_reasons:
            best_reason = "fire"
        else:
            best_reason = min(
                self._episode_reasons,
                key=lambda r: _REASON_RANK.get(r.split(":")[0], 99),
            )
        summary = {
            "frames": self._episode_frames,
            "dropped": self._episode_dropped,
            "max_v": round(self._episode_best[0], 2) if self._episode_best else 0.0,
            "max_travel": round(self._episode_best[1], 2) if self._episode_best else 0.0,
            "verdict": best_reason,
        }
        self._episode_open = False
        self._episode_frames = 0
        self._episode_dropped = 0
        self._episode_best = None
        self._episode_reasons = []
        return summary

    # -- main update --------------------------------------------------------
    def update(self, lm, now: float) -> Actions:
        act = Actions()

        # A timed sequence (tap/long-press/legacy replay) is in flight on the
        # sequencer's slot — don't fight it with live touch input from the
        # same frame's landmarks. The cursor is a separate uinput device from
        # the touch it drives, so it's allowed to keep moving/visible while a
        # synthesized sequence plays on slot 1 — only the live touch (slot 0)
        # and arming a new gesture are suppressed.
        if self.sequencer is not None and self.sequencer.busy:
            self._release_touch(act)
            if lm is not None:
                act.cursor = self._map_cursor(lm)
            act.state = "sequence"
            return act

        if lm is None:
            self._release_touch(act)
            self._touch_streak = self._long_press_streak = 0
            if self._palm_streak or (now - self._last_palm_t) <= self._palm_grace_s:
                self._episode_dropped += 1
            # Still feed the edge-drag tracker even with no hand at all — an
            # in-flight drag has its own lost-hand grace window and must be
            # able to release cleanly rather than hang forever.
            edge_event = self.edge_tracker.update(None, False, False, now)
            if edge_event is not None:
                act.edge_drag = edge_event
            act.state = "edge-drag" if self.edge_tracker.active else "no-hand"
            return act

        scale = _dist(lm[WRIST], lm[MIDDLE_MCP]) or 1e-6
        pinch_index = _dist(lm[INDEX_TIP], lm[THUMB_TIP]) / scale
        pinch_middle = _dist(lm[MIDDLE_TIP], lm[THUMB_TIP]) / scale
        act.debug = {"pinch_index": round(pinch_index, 2), "pinch_middle": round(pinch_middle, 2)}
        act.cursor = self._map_cursor(lm)

        # Motion history is sampled every frame, unconditionally — see
        # _record_motion's docstring for why this can't be gated on posture.
        self._record_motion(lm, scale, now)

        palm_ok, palm_debug = self._open_palm(lm, scale)
        act.debug.update(palm_debug)
        if palm_ok:
            self._palm_streak += 1
            self._last_palm_t = now
        else:
            self._palm_streak = 0

        armed = (
            self._palm_streak >= self.swipe_arm_frames
            or (now - self._last_palm_t) <= self._palm_grace_s
        )

        # Live-follow edge-drag: fed every frame that has a hand, with its
        # own independent arm/drag/cooldown state machine (edgedrag.py). A
        # no-op every frame when disabled (cfg [edge_swipe].enabled=false),
        # which keeps this whole path a no-op and everything below behaving
        # exactly as it did before edge-drag existed.
        edge_event = self.edge_tracker.update(act.cursor, palm_ok, self._palm_hold(lm), now)
        if edge_event is not None:
            act.edge_drag = edge_event

        nav_mode = armed or palm_ok or self.edge_tracker.phase is not EdgePhase.IDLE

        if nav_mode:
            # Open palm (or recently open, or an edge-drag in flight):
            # navigation mode. A pinch is physically impossible with the hand
            # open, so this never competes with touch/long-press while truly
            # open; the grace window only extends swipe *detection*, not
            # touch suppression.
            self._release_touch(act)
            self._touch_streak = self._long_press_streak = 0
            if armed and not self.edge_tracker.active:
                direction, reason, v, travel = self._detect_swipe(now)
                act.debug["swipe_reject"] = reason
                act.debug["v"] = round(v, 2)
                act.debug["travel"] = round(travel, 2)
                self._episode_update(reason, v, travel)
                # up/down are owned by the live-follow tracker once it's
                # enabled for them — drop the legacy replay's result rather
                # than also firing on the same TouchSequencer slot.
                owned_by_edge_drag = (
                    direction in ("up", "down")
                    and self.edge_tracker.enabled
                    and direction in self.edge_tracker.directions
                )
                act.swipe = None if owned_by_edge_drag else direction
                if reason == "fire":
                    closed = self._episode_close()
                    if closed:
                        act.debug["episode"] = closed
            if self.edge_tracker.active:
                act.state = "edge-drag"
            elif self.edge_tracker.phase is EdgePhase.ARMED:
                act.state = "armed"
            else:
                act.state = "palm" if palm_ok else "grace"
            return act

        # Hand is in a non-palm, non-grace posture: any swipe attempt that
        # was in flight has ended without firing — close out its summary.
        closed = self._episode_close()
        if closed:
            act.debug["episode"] = closed

        # Live touch: index+thumb pinch, with hysteresis (engage < release,
        # N-frame streak to engage, immediate on release).
        if not self.touch_down:
            self._touch_streak = self._touch_streak + 1 if pinch_index < self.engage else 0
            if self._touch_streak >= self.engage_frames:
                self.touch_down = True
                act.touch = True
                self._touch_streak = 0
        else:
            if pinch_index > self.release:
                self.touch_down = False
                act.touch = False

        if self.touch_down:
            act.touch_at = act.cursor

        # Long-press: middle+thumb pinch, only while not already touching.
        # Require the middle pinch to be clearly tighter than the index
        # pinch so a thumb resting between the two fingers doesn't trigger
        # both gestures at once.
        if not self.touch_down:
            middle_wins = pinch_middle < pinch_index * 0.8
            self._long_press_streak = (
                self._long_press_streak + 1
                if pinch_middle < self.engage and middle_wins
                else 0
            )
            if (self._long_press_streak >= self.engage_frames
                    and now - self._last_long_press > self.long_press_cooldown):
                act.long_press = True
                act.touch_at = act.cursor
                self._last_long_press = now
                self._long_press_streak = 0
        else:
            self._long_press_streak = 0

        act.state = "touch" if self.touch_down else "point"
        return act
