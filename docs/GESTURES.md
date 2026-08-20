# Gestures

One hand in front of the webcam. All distances are normalized by hand size —
it works close to or far from the camera.

The engine emulates a genuine **touchscreen** (multi-touch,
`INPUT_PROP_DIRECT`), not a mouse. A second virtual device — a buttonless
absolute pointer — only draws an aim cursor, since a touchscreen has no
hover: without it the user would be "blind" about where the touch will land
before closing the pinch.

| Gesture | Action |
|---|---|
| Move the hand (pointing) | Moves the aim cursor (anchored to the index finger's base joint) |
| Index + thumb pinch | Touch on screen: close = touch down, open = touch up. Hold and move = drag/scroll (native GTK scrolling, with inertia) |
| Middle + thumb pinch (only if not already touching) | Long press (~0.7s) at the current point — context menu / selection |
| Open hand (5 fingers) + upward move | Bottom-edge **drag**, follows the hand live — home / app grid / unlock |
| Open hand + downward move | Top-edge **drag**, follows the hand live — notifications / quick settings |
| Open hand + fast lateral move | Lateral swipe (legacy, see below) — app grid pages |
| Hand out of frame | Cursor freezes; any active touch is released |

Up/down are **edge drags**, not swipes: a touch tracks the hand live, the
same way the pinch-drag cursor does, instead of guessing after the fact
whether a motion "was a swipe" and replaying a canned animation. See "Edge
drag" below. Left/right still use the older approach — classify the wrist
motion, then replay a fixed touch path (`TouchSequencer`) — because the
lateral gesture phoc/Phosh actually recognizes for app-grid paging hasn't
been confirmed yet (see `ROADMAP.md`).

## Anti-jitter

- **One Euro filter** on the aim cursor: steady when still, responsive when
  moving.
- **Hysteresis** on the touch pinch: engages at `pinch_engage` (0.40), only
  releases above `pinch_release` (0.55), and must persist for
  `engage_frames` (3) frames.
- **Index/middle de-conflict**: the middle pinch only counts if it's
  noticeably tighter than the index pinch
  (`pinch_middle < pinch_index * 0.8`), so a thumb resting between the two
  fingers doesn't trigger touch and long-press at the same time.
- **Cooldowns**: `swipe_cooldown` (0.8s) and `long_press_cooldown` (1.0s)
  prevent repeated triggers of the same gesture.
- **Active region**: only the center of the camera frame (15%–85%) is mapped
  onto the full screen — the hand never has to reach the edges.

## Edge drag (up/down)

`edgedrag.py` + `touchstream.py`. Investigated after real usage kept
reporting swipes that "just don't fire" — turned out the *synthesized touch*
was never the problem (it already covered 64% of the screen, well past what
phoc requires), the *classifier deciding whether to fire it* was: it
frequently rejected genuine fast flicks because MediaPipe loses hand
tracking at exactly the peak velocity of a flick (motion blur), discarding
the most informative sample right when the travel/velocity gates need it
most.

phoc (the compositor, not Phosh) recognizes home/quick-settings via a
Wayland protocol built for exactly this: a surface that follows a touch in
real time and decides on release whether to fold or unfold — either the
touch crossed 30% of the total distance, or it released fast enough to
count as a fling (≥1500px/s measured over the last 150ms of events; both
figures are logical pixels — this VM's `scale=2`, so px means half the
QEMU display's actual pixels — and were calibrated **empirically** on this
VM's phoc 0.46.0, not just read off phoc's source, since the two can drift).

So instead of "wait for the whole gesture, judge it, replay a fixed path if
it passes": open palm + a small amount of vertical motion (`start_slop`,
screen-normalized, roughly Android's `touch_slop` in spirit) arms a drag; the
touch goes down **anchored exactly on the screen edge** (not wherever the
hand happens to be — phoc's own edge-recognition needs the touch-down inside
a handle only a few logical pixels wide, calibrated on the VM and recorded
in `ROADMAP.md`); from there, only the hand's *delta* since arming drives the
touch, with the horizontal axis **frozen** (`axis_lock`) so hand jitter can
never trip phoc's own reject-on-lateral-movement check. The hand losing palm
posture (including from flick-induced motion blur) or disappearing releases
the touch — phoc, not this engine, decides what that meant.

A resampler (`touchstream.py`) re-emits the touch at ~120Hz between camera
frames (interpolating/extrapolating from the last two hand positions),
because the camera's ~15fps is too coarse for phoc's 150ms fling-velocity
window on its own — see the module's docstring for why the constant here is
deliberately much larger than a typical touchscreen-rate resampler's.

One real behavior change from the old replay-based swipe: a short, slow
flick that used to always fire the full canned animation regardless of how
far the hand actually moved now only opens the panel if the *live* drag
clears phoc's own thresholds. That's the point — it's what a real
touchscreen does — but it means "it fires" and "it visibly opens something"
are no longer the same event; use the `edge-drag ... frames=... moves=...`
log line (see below) together with a screenshot, not the fire count alone,
to judge whether a change actually helped.

## Legacy swipe detection (left/right, and the voice/effector path)

Wrist-motion history is sampled on **every** frame that has a detected hand,
regardless of the posture recognized in that frame — it's exactly at the
peak velocity of a flick that motion blur makes MediaPipe fail to confirm
"open palm". A "grace" window (`palm_grace_frames`, 3 frames) keeps the
swipe armed for a moment after the posture stops being recognized, so
dropping 1-2 frames mid-gesture doesn't kill the swipe.

Firing gates, in order: `swipe_min_travel` (minimum wrist displacement, in
hand-scales) → `swipe_velocity` (minimum velocity — in practice unreachable
as an independent gate given the packaged `swipe_window`, see the comments
in `config.toml`) → `swipe_axis_ratio` (axis dominance, corrected for the
camera's aspect distortion before comparing x/y). Each rejection produces a
reason (`too-short`, `too-slow`, `diagonal`, `cooldown`, ...) — see the
logging section below.

This path still owns left/right, and is still what `shell.swipe` uses for
every direction when driven from voice/effector (there's no hand in frame
to follow live in that case) — it replays a fixed touch path via
`TouchSequencer`, same as before this redesign.

## Tuning

`/etc/musashi/config.toml` on the guest overrides the package defaults
(`[gestures]`, `[touch]`, `[edge_swipe]`/`[edge_swipe.stream]` sections, key
by key — see the comments in `config.toml` for what each knob does; most of
the edge-drag ones are already the values calibrated live on this VM's phoc
0.46.0, recorded in `ROADMAP.md`, and shouldn't need re-tuning unless the
image is rebuilt against a different phoc version). Restart the engine after
editing: `pkill -f musashi_gestures` (Phosh's XDG autostart relaunches it on
the next login, or run
`/opt/gesture-engine/venv/bin/python -m musashi_gestures --no-overlay &`).

`./build/update-gesture-engine.sh` (without `--overlay`) overwrites
`/etc/musashi/config.toml` with the package's `config.toml` on every run —
copy tuned values back to the repo before running it again, otherwise manual
adjustments made on the guest are lost.

## Log

`/tmp/gesture-engine.log` (rotated, up to ~3MB). Defaults to INFO — the
autostart entry doesn't pass `-v`, so `log.debug(...)` stays silent; use
`--log-file ""` to disable the file and only log to stderr, or `-v` for
extra detail.

Three layers, all at INFO:

1. **Per frame** (rate-limited to ~4Hz, only while near an open-palm
   posture): shows which fingers count as extended, thumb abduction, and —
   if a swipe attempt happened that frame — the rejection reason with its
   measured value.
2. **Per episode** (one line per swipe attempt): `palm-episode end
   frames=N dropped=N max_v=X max_travel=X verdict=<reason>` — the reason
   is from whichever frame came closest to firing, not the last frame.
3. **Every 5s**: aggregate counters (`palm_frames`, `hand_lost`, `fired`,
   `edge_drag_released`, `edge_drag_cancelled`, and a `rej{...}` histogram by
   rejection reason for the legacy left/right path), on the same line as fps.

Edge drags (up/down) log their own two lines, independent of the three
layers above: `edge-drag down dir=<up|down>` when the touch is pressed, and
`edge-drag <up|cancel> dir=... frames=N dur=Xs keyframes=N moves=N
predicted=N held=N` when it's released — `moves` is how many `touch.move()`
calls the resampler actually emitted (compare against
`libinput debug-events` on the guest to sanity-check the resampler is really
delivering more than the camera's raw ~15fps would).

If the log shows a gesture firing (`swipe %s`, or an edge-drag with a
plausible `moves` count) but nothing happens on screen, the problem is no
longer the gesture engine — it has moved to phoc/Phosh's own acceptance
logic. See `ROADMAP.md` for the phoc output-name validation and the
edge-drag threshold calibration, both done by driving `TouchInjector`
directly over SSH and comparing `grim` screenshots before/after.
