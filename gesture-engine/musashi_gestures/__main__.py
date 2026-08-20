"""MusashiOS gesture engine daemon.

Pipeline: webcam frame -> MediaPipe Hands -> gesture state machine -> uinput.
Config: /etc/musashi/config.toml overrides the packaged defaults.
"""
import argparse
import importlib.resources
import logging
import logging.handlers
import signal
import sys
import time
import tomllib

from .camera import Camera
from .gestures import GestureStateMachine
from .hands import HandTracker
from .injector import PointerInjector, TouchInjector
from .intents import Arg, Intent, Registry, ToolClass
from .sequencer import TouchSequencer
from .smoothing import PointFilter

log = logging.getLogger("musashi-gestures")

SYSTEM_CONFIG = "/etc/musashi/config.toml"
DEFAULT_LOG_FILE = "/tmp/gesture-engine.log"

# How often (seconds) to emit a Layer-1 per-frame diagnostic line while the
# hand is in/near a palm posture. The autostart entry never passes -v, so
# this is INFO-level and must stay sparse enough to not flood the log.
_PALM_LOG_INTERVAL = 0.25


def load_config() -> dict:
    data = importlib.resources.files("musashi_gestures").joinpath("config.toml").read_bytes()
    cfg = tomllib.loads(data.decode())
    try:
        with open(SYSTEM_CONFIG, "rb") as f:
            for section, values in tomllib.load(f).items():
                cfg.setdefault(section, {}).update(values)
    except FileNotFoundError:
        pass
    return cfg


def _setup_logging(verbose: bool, log_file: str) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    root = logging.getLogger()
    root.setLevel(level)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_file:
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=2
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError:
            # Logging must never be able to take the engine down — fall back
            # to stderr-only and keep going.
            log.warning("could not open log file %s, logging to stderr only", log_file)


def _log_palm_frame(act, now: float, state: dict) -> None:
    """Layer 1: rate-limited per-frame diagnostic while near a palm posture."""
    if now - state["t"] < _PALM_LOG_INTERVAL:
        return
    state["t"] = now
    d = act.debug
    ext = d.get("ext")
    if ext is None:
        return
    ext_str = " ".join(f"{k}{int(v)}" for k, v in ext.items())
    n = sum(ext.values())
    parts = [f"palm ext={ext_str} ({n}/4) thumb={d.get('thumb')}"]
    if "swipe_reject" in d:
        parts.append(f"reject={d['swipe_reject']}")
    log.info(" ".join(parts))


def _log_episode(act) -> None:
    """Layer 2: one line per completed swipe attempt."""
    ep = act.debug.get("episode")
    if not ep:
        return
    log.info(
        "palm-episode end frames=%d dropped=%d max_v=%.2f max_travel=%.2f verdict=%s",
        ep["frames"], ep["dropped"], ep["max_v"], ep["max_travel"], ep["verdict"],
    )


def _build_gesture_registry(touch, sequencer, machine) -> Registry:
    """In-process tool table for the gesture engine.

    Only *discrete* gestures are dispatched through it. Cursor motion and the
    live pinch touch stay inline in the loop on purpose: they are a continuous
    input stream at camera framerate, not proposable commands, and running
    them through per-frame schema validation would cost time in the hot loop
    for no safety gain. `Actions` is still the state machine's output — it
    just stopped being the dispatch contract.

    Coordinates are not range-checked here (unlike the effector's `ui.tap`):
    they come from the local state machine, and `injector._clamp` already
    handles MediaPipe landmarks that stray slightly outside [0, 1]. Adding
    bounds here would silently drop otherwise-valid long presses.
    """
    reg = Registry()

    def _swipe(dir: str):
        # Belt-and-braces: guarantee the synthesized swipe (slot 1) is never
        # joined by a stuck live-touch contact (slot 0) — a no-op if slot 0
        # is already up.
        touch.up(0)
        sequencer.edge_swipe(dir)
        return dir

    def _long_press(x: float, y: float):
        sequencer.long_press(x, y, machine.long_press_hold)
        return None

    reg.register(
        "shell.swipe", ToolClass.EFFECT,
        {"dir": Arg(str, choices=("up", "down", "left", "right"))},
        _swipe, doc="Edge swipe: home / notifications / app pages.",
    )
    reg.register(
        "ui.long_press", ToolClass.EFFECT,
        {"x": Arg(float), "y": Arg(float)},
        _long_press, doc="Long press (secondary action) at a normalized point.",
    )
    return reg


def main() -> int:
    parser = argparse.ArgumentParser(prog="musashi-gestures")
    parser.add_argument("--no-overlay", action="store_true", help="disable preview window")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE,
                         help="path to log file (empty string disables file logging)")
    args = parser.parse_args()

    _setup_logging(args.verbose, args.log_file)
    cfg = load_config()
    log.info("config: gestures=%s cursor=%s", cfg["gestures"], cfg["cursor"])

    cam = Camera(**cfg["camera"])
    tracker = HandTracker(**cfg["hands"])
    pointer = PointerInjector()
    touch = TouchInjector()
    sequencer = TouchSequencer(touch, **cfg["touch"])
    machine = GestureStateMachine(cfg, sequencer)
    smoother = PointFilter(**cfg["smoothing"])
    registry = _build_gesture_registry(touch, sequencer, machine)
    log.info("intent registry: %s", registry.names())

    overlay = None
    if cfg["overlay"]["enabled"] and not args.no_overlay:
        from .overlay import Overlay
        overlay = Overlay(cfg["overlay"]["scale"])

    running = True

    def stop(_sig, _frm):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log.info("gesture engine started")
    frames, t_report = 0, time.monotonic()
    palm_log_state = {"t": 0.0}
    counters = {"palm_frames": 0, "hand_lost": 0, "fired": 0, "rej": {}}
    try:
        while running:
            frame = cam.read()
            if frame is None:
                continue
            now = time.monotonic()
            landmarks = tracker.process(frame)
            act = machine.update(landmarks, now)

            pos = None
            if act.cursor is not None:
                pos = smoother.apply(*act.cursor, now)
                pointer.move(*pos)

            if act.touch is True:
                touch.down(0, *(pos if pos is not None else act.touch_at))
                log.debug("touch down")
            elif act.touch is False:
                touch.up(0)
                log.debug("touch up")
            elif act.touch_at is not None and machine.touch_down:
                touch.move(0, *(pos if pos is not None else act.touch_at))

            # Discrete gestures become Intents and go through the registry;
            # the registry, not this loop, decides whether they run.
            proposals = []
            if act.long_press:
                lx, ly = pos if pos is not None else act.touch_at
                proposals.append(Intent("ui.long_press", {"x": lx, "y": ly}, source="gesture"))
            if act.swipe:
                proposals.append(Intent("shell.swipe", {"dir": act.swipe}, source="gesture"))

            for intent in proposals:
                res = registry.dispatch(intent)
                if not res.ok:
                    log.warning("intent rejected: %s — %s", intent, res.error)
                elif intent.name == "shell.swipe":
                    log.info("swipe %s", intent.args["dir"])
                    counters["fired"] += 1
                else:
                    log.debug("long press")

            if act.state in ("palm", "grace"):
                counters["palm_frames"] += 1
                _log_palm_frame(act, now, palm_log_state)
                reason = act.debug.get("swipe_reject")
                if reason and reason != "fire":
                    key = reason.split(":")[0]
                    counters["rej"][key] = counters["rej"].get(key, 0) + 1
            elif act.state == "no-hand":
                counters["hand_lost"] += 1

            _log_episode(act)

            if overlay:
                overlay.show(frame, landmarks, act.state)

            frames += 1
            if now - t_report >= 5.0:
                rej_str = " ".join(f"{k}:{v}" for k, v in counters["rej"].items())
                log.info(
                    "fps=%.1f state=%s %s palm_frames=%d hand_lost=%d fired=%d rej{%s}",
                    frames / (now - t_report), act.state, act.debug,
                    counters["palm_frames"], counters["hand_lost"], counters["fired"], rej_str,
                )
                frames, t_report = 0, now
                counters = {"palm_frames": 0, "hand_lost": 0, "fired": 0, "rej": {}}
    finally:
        if overlay:
            overlay.close()
        sequencer.stop()
        touch.close()
        pointer.close()
        tracker.close()
        cam.close()
        log.info("gesture engine stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
