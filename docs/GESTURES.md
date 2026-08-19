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
| Open hand (5 fingers) + fast upward move | Bottom-edge swipe upward — home / app grid / unlock |
| Open hand + fast downward move | Top-edge swipe downward — notifications / quick settings |
| Open hand + fast lateral move | Lateral swipe — app grid pages |
| Hand out of frame | Cursor freezes; any active touch is released |

Swipes are triggered by wrist velocity (normalized by hand size), not by
position — the hand can be anywhere in the frame, it just needs to move fast
enough with an open palm. Internally, a swipe is synthesized as a
self-timed touch sequence (~150ms, starting near the screen edge) by a
`TouchSequencer` on a separate thread — the main camera-capture loop never
blocks waiting for it.

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

## Swipe detection

Wrist-motion history is sampled on **every** frame that has a detected hand,
regardless of the posture recognized in that frame — it's exactly at the
peak velocity of a flick that motion blur makes MediaPipe fail to confirm
"open palm". A "grace" window (`palm_grace_frames`, 3 frames) keeps the
swipe armed for a moment after the posture stops being recognized, so
dropping 1-2 frames mid-gesture doesn't kill the swipe.

Firing gates, in order: `swipe_min_travel` (minimum wrist displacement, in
hand-scales) → `swipe_velocity` (minimum velocity, deliberately generous —
see the comments in `config.toml`) → `swipe_axis_ratio` (axis dominance,
corrected for the camera's aspect distortion before comparing x/y). Each
rejection produces a reason (`too-short`, `too-slow`, `diagonal`,
`cooldown`, ...) — see the logging section below.

## Tuning

`/etc/musashi/config.toml` on the guest overrides the package defaults
(`[gestures]` and `[touch]` sections, key by key). Restart the engine after
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
   and a `rej{...}` histogram by rejection reason), on the same line as fps.

If the log shows `swipe %s` firing but nothing happens on screen, the
problem is no longer the gesture engine — it has moved to phoc (see
`ROADMAP.md`'s pending-validation section, about the `Virtual-1` output name
in `phoc.ini`).
