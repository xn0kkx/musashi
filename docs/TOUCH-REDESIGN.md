> **Rebrand note (2026-08-19):** this document is historical — it records
> the decisions from the touchscreen-rewrite session under the project's old
> name (`HaikuOS`, guest user `haiku`, package `haiku_gestures`). The
> project was renamed to **MusashiOS** and had its target redefined (a
> multi-LLM mainframe with `gesture-engine` as one surface's driver, no
> longer the center of the architecture) — see `Plano-Diretor.md` in
> Obsidian (`1-Projects/MusashiOS/`). The names below (`haiku`,
> `haiku_gestures`, `/etc/haikuos`) reflect the code as it was *in that
> session*; the current code already uses
> `musashi`/`musashi_gestures`/`/etc/musashi`.

Project: HaikuOS — a mini Linux OS (Debian minbase) controlled by hand
gestures via webcam, running isolated in a QEMU/KVM VM. Location:
/mnt/n0kk_storage/Projects/haikuos

STATUS: implemented (see docs/GESTURES.md for the final gesture semantics).
This file remains as a record of the original brief and the decisions that
were settled. Only in-VM validation is left (build + boot + manual test).

## Prior state (working, BEFORE the touchscreen session)

- Guest: Debian 13 trixie (debootstrap minbase), direct boot via QEMU
  -kernel/-initrd.
- Real webcam (USB passthrough) → OpenCV → MediaPipe Hands (Tasks API,
  gesture-engine/haiku_gestures/hands.py) detects the hand and runs at
  ~15fps.
- GUI: Phosh (Android-style Wayland shell) on top of the phoc compositor.
  Autologin as the `haiku` user on tty1 brings up phosh-session
  automatically. Lock screen disabled via dconf
  (sm.puri.phosh.lockscreen require-unlock=false).
- Input injection: gesture-engine/haiku_gestures/injector.py creates a
  /dev/uinput device emulating a MOUSE/TABLET with absolute positioning —
  ABS_X/ABS_Y (range 0-65535), BTN_LEFT/BTN_RIGHT/BTN_MIDDLE, REL_WHEEL for
  scroll.
- gestures.py: a state machine that at the time recognized: cursor (index
  finger position), index+thumb pinch = left click/drag, middle+thumb pinch
  = right click, index+middle extended = scroll. Everything designed as a
  MOUSE, not touch.
- Build: build/build-image.sh (full rebuild, debootstrap+everything,
  ~45min) and build/update-gesture-engine.sh (fast path — only syncs
  gesture-engine/ and reinstalls into the venv via NBD mount, ~1min, for
  fast iteration without a full rebuild).
- run.sh boots the VM: `qemu-system-x86_64 ... -device virtio-vga -display
  gtk,gl=off -device qemu-xhci,id=xhci -device usb-host,...` (real webcam
  passthrough, IDs 6211:e904 "XIFT Web Camera").

## This session's goal

### 1. Replace mouse emulation with a real multi-touch touchscreen

Phosh was designed for touch (swipe to unlock, swipe down from the top opens
notifications, tap to open apps) — today we're emulating a MOUSE/tablet,
which is the likely cause of interactions not working as expected. Needs:

- Rewrite injector.py to create a uinput device as a real TOUCHSCREEN:
  `INPUT_PROP_DIRECT`, multi-touch protocol B (`ABS_MT_SLOT`,
  `ABS_MT_TRACKING_ID`, `ABS_MT_POSITION_X`, `ABS_MT_POSITION_Y`, plus
  `BTN_TOUCH`), no longer absolute-pointer ABS_X/ABS_Y.
- Redesign gestures.py to recognize and emit TOUCH events instead of
  mouse click/drag:
  - 1-finger touch (tap) → single tap on screen (open app, press button).
  - 2-finger touch → secondary action (decide: long-press? back? context
    menu?).
  - Drag up / down / left / right → Android-style navigation gestures (up
    = home/app grid, down = notifications, sides = switch app or go back —
    decide the exact mapping).
  - Keep scroll and drag wherever they make sense within this new touch
    semantics.
- Define which HAND gesture (not screen gesture) triggers each touch action
  — this is a design decision that needs discussion: e.g., "point and tap
  the air with 1 finger" = tap; "two-finger V" = 2-finger touch; fast hand
  motion in a direction = swipe in that direction. Work this out together
  in chat.

### 2. VM fullscreen in QEMU

run.sh currently boots with `-display gtk,gl=off` in a window. Switch to a
real fullscreen (`-full-screen` in QEMU, or equivalent), with the guest
resolution adjusted (phoc.ini) to cleanly fill the host screen, with no
leftover QEMU UI (Machine/View menu) getting in the way.

## udev classification trap (found while testing on the host)

uinput device classification in udev/libinput is **by declared capability,
not by device name and not by emitted events**. A pointer that only
declares `ABS_X`/`ABS_Y`, with no `EV_KEY` at all, **doesn't** become
`ID_INPUT_MOUSE=1` — it's left with only the generic `ID_INPUT=1`, and Phosh
probably draws no cursor for it at all. Confirmed by testing on the host:
creating `PointerInjector` with only ABS axes and reading it back via
`evdev.InputDevice` + `udevadm info <path>` (after `udevadm settle` — it
takes a moment to populate the database), `ID_INPUT_MOUSE` didn't show up.
Fixed by declaring `EV_KEY: [BTN_LEFT]` in `PointerInjector`'s
capabilities — never actually emitted (the device only moves the cursor,
never clicks), only declared, and that's enough for classification.
`TouchInjector`, with `INPUT_PROP_DIRECT` + `ABS_MT_*` axes, already came
out correctly classified as `ID_INPUT_TOUCHSCREEN=1` + `TAGS=:seat:`
without needing this adjustment.

Moral for any new uinput device in this project: always actually inspect
`udevadm info <path>` (don't assume the "right" axes are enough) — this can
be done on the host, without root for the protocol
(`gesture-engine/tests/test_injector_frames.py`, with a `FakeUInput` that
records writes) and with root only to read real capabilities without
emitting events.

## Important caution (learned this session)

The host desktop has several overlapping windows from OTHER projects
(including sessions from another chat/project running in terminals).
Automating clicks via xdotool at real desktop coordinates is DANGEROUS — a
"blind" click has already hit OBS Studio once, and another hit a terminal
tab from an unrelated project (Instagram bug fix). DO NOT automate tests by
clicking on the host desktop. To test the VM in isolation, use VNC (`-vnc
:N` instead of `-display gtk`) or ask the user to test manually inside the
VM's own window/fullscreen.
