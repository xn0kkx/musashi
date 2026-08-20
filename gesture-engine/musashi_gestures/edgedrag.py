"""Live-follow edge-drag state machine: open palm + vertical motion -> a
touch that tracks the hand, anchored at the screen edge, letting phoc itself
decide whether the gesture opens (30% of the drag distance, or a fast enough
fling) or falls back (see docs/GESTURES.md "Edge drag").

This replaces "classify after the fact, replay a fixed path" (the legacy
_detect_swipe/_SWIPE_PATHS pair in gestures.py/sequencer.py, kept for
tap/long-press and the voice/effector shell.swipe path) with a continuous
stream, the same way the live pinch-drag already works: a touch down as soon
as the gesture looks intentional, a touch move every frame following the
hand's delta, a touch up when the hand releases. The compositor sees exactly
what it would see from a real finger.

Pure state machine, no cv2/evdev/mediapipe — operates on screen-normalized
[0,1] coordinates (the same `act.cursor` mapping the live cursor/pinch-drag
already use), not hand-scale units. Testable on the host with synthesized
(x, y) sequences, no camera required.
"""
import enum
from dataclasses import dataclass


class EdgePhase(str, enum.Enum):
    IDLE = "idle"
    ARMED = "armed"
    DRAGGING = "dragging"
    COOLDOWN = "cooldown"


@dataclass(frozen=True)
class EdgeDragEvent:
    phase: str        # "down" | "move" | "up" | "cancel"
    x: float
    y: float
    dir: str           # "up" | "down"
    t: float


class EdgeDragTracker:
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True)
        self.directions = set(cfg.get("directions", ["up", "down"]))
        self.arm_frames = cfg.get("arm_frames", 2)
        self.arm_window = cfg.get("arm_window", 0.40)
        self.start_slop = cfg.get("start_slop", 0.06)
        self.axis_margin = cfg.get("axis_margin", 0.03)
        self.follow_gain = cfg.get("follow_gain", 1.0)
        self.axis_lock = cfg.get("axis_lock", True)
        self.edge_inset = cfg.get("edge_inset", 0.006)
        self.x_anchor_min = cfg.get("x_anchor_min", 0.20)
        self.x_anchor_max = cfg.get("x_anchor_max", 0.80)
        self.release_grace = cfg.get("release_grace", 0.13)
        self.lost_grace = cfg.get("lost_grace", 0.13)
        self.max_duration = cfg.get("max_duration", 2.5)
        self.cooldown = cfg.get("cooldown", 0.30)

        self._phase = EdgePhase.IDLE
        self._palm_streak = 0
        self._arm_t = 0.0
        self._arm_xy = None

        self._x_anchor = 0.0
        self._y_edge = 0.0
        self._dir = None
        self._start_t = 0.0
        self._last_seen_t = 0.0
        self._last_hold_t = 0.0
        self._cooldown_until = -1e9
        self._last_xy = None

        self._episode_frames = 0

    @property
    def phase(self) -> EdgePhase:
        return self._phase

    @property
    def active(self) -> bool:
        return self._phase is EdgePhase.DRAGGING

    # -- internals --------------------------------------------------------
    def _clamp(self, v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    def _target_y(self, xy) -> float:
        dy = (xy[1] - self._arm_xy[1]) * self.follow_gain
        return self._clamp(self._y_edge + dy, 0.0, 1.0)

    def _begin_drag(self, xy, direction: str, now: float) -> EdgeDragEvent:
        self._phase = EdgePhase.DRAGGING
        self._dir = direction
        self._x_anchor = self._clamp(xy[0], self.x_anchor_min, self.x_anchor_max)
        self._y_edge = (1.0 - self.edge_inset) if direction == "up" else self.edge_inset
        self._start_t = now
        self._last_seen_t = now
        self._last_hold_t = now
        self._episode_frames = 1
        # The DOWN itself lands exactly on the edge (delta = 0), not offset
        # by the slop already covered while ARMED: the distance-based path
        # in phoc requires the touch-down to fall inside the drag handle,
        # which is only a few px wide (see the module docstring). The
        # accumulated slop is delivered on the very next MOVE instead — that
        # single jump alone already clears phoc's accept-distance threshold,
        # so there's no dead zone, just a one-frame-deep "down" before motion
        # starts (origin_mode = "arm": _target_y measures from arm_xy, not
        # from this down position).
        self._last_xy = (self._x_anchor, self._y_edge)
        return EdgeDragEvent(phase="down", x=self._x_anchor, y=self._y_edge, dir=direction, t=now)

    def _release(self, now: float, cancelled: bool = False) -> EdgeDragEvent:
        self._phase = EdgePhase.COOLDOWN
        self._cooldown_until = now + self.cooldown
        d = self._dir
        x, y = self._last_xy if self._last_xy else (self._x_anchor, self._y_edge)
        self._dir = None
        return EdgeDragEvent(
            phase="cancel" if cancelled else "up",
            x=x, y=y, dir=d, t=now,
        )

    # -- main update --------------------------------------------------------
    def update(self, screen_xy, palm_ok: bool, palm_hold_ok: bool, now: float):
        """One call per camera frame. `screen_xy` is `act.cursor` (or None if
        no hand this frame) — screen-normalized [0,1], same mapping the live
        cursor uses. Returns an EdgeDragEvent or None.
        """
        if not self.enabled:
            return None

        if self._phase is EdgePhase.COOLDOWN:
            if now >= self._cooldown_until:
                self._phase = EdgePhase.IDLE
            else:
                return None

        if self._phase is EdgePhase.IDLE:
            if screen_xy is not None and palm_ok:
                self._palm_streak += 1
            else:
                self._palm_streak = 0
            if self._palm_streak >= self.arm_frames:
                self._phase = EdgePhase.ARMED
                self._arm_t = now
                self._arm_xy = screen_xy
                self._palm_streak = 0
            return None

        if self._phase is EdgePhase.ARMED:
            if screen_xy is None:
                self._phase = EdgePhase.IDLE
                return None
            if now - self._arm_t > self.arm_window:
                self._phase = EdgePhase.IDLE
                return None

            dx = screen_xy[0] - self._arm_xy[0]
            dy = screen_xy[1] - self._arm_xy[1]
            adx, ady = abs(dx), abs(dy)

            if ady - adx >= self.axis_margin and ady >= self.start_slop:
                direction = "down" if dy > 0 else "up"
                if direction in self.directions:
                    return self._begin_drag(screen_xy, direction, now)
                # A vertical gesture we're not configured to handle (e.g.
                # "down" while only "up" is enabled during a staged rollout)
                # — let it go cold rather than sit ARMED indefinitely.
                self._phase = EdgePhase.IDLE
                return None
            if adx - ady >= self.axis_margin and adx >= self.start_slop:
                # Horizontal-dominant: not ours. The legacy classifier in
                # gestures.py runs off its own independent motion buffer and
                # doesn't need anything from us — just stand down.
                self._phase = EdgePhase.IDLE
                return None
            return None

        # DRAGGING
        if now - self._start_t > self.max_duration:
            return self._release(now, cancelled=True)

        if screen_xy is None:
            if now - self._last_seen_t > self.lost_grace:
                return self._release(now)
            return None

        self._last_seen_t = now
        if palm_hold_ok:
            self._last_hold_t = now
        elif now - self._last_hold_t > self.release_grace:
            return self._release(now)

        self._episode_frames += 1
        y = self._target_y(screen_xy)
        self._last_xy = (self._x_anchor, y)
        return EdgeDragEvent(phase="move", x=self._x_anchor, y=y, dir=self._dir, t=now)

    def abort(self, now: float):
        """Force-release an in-flight drag (engine shutdown, mode switch).
        Returns an EdgeDragEvent (phase="cancel") if a touch was actually
        down, else None.
        """
        if self._phase is EdgePhase.DRAGGING:
            return self._release(now, cancelled=True)
        self._phase = EdgePhase.IDLE
        self._palm_streak = 0
        return None

    def take_episode(self) -> dict | None:
        """One-shot summary of the drag that just ended, for the log — call
        right after `update`/`abort` returns an "up"/"cancel" event.
        """
        if self._episode_frames == 0:
            return None
        summary = {
            "frames": self._episode_frames,
            "duration": round(max(0.0, self._last_seen_t - self._start_t), 3),
        }
        self._episode_frames = 0
        return summary
