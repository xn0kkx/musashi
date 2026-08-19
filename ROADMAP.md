# Roadmap

MusashiOS project state (old name: HaikuOS). Updated 2026-08-19.

The detailed pivot roadmap (sprints S0–S14 of the main path and the parallel
空 KŪ track) lives in the **Plano Diretor** (Obsidian, `1-Projects/MusashiOS/
Plano-Diretor.md`). This file only records the concrete state of the code in
this repository.

## Done (inherited from the HaikuOS prototype, already renamed to MusashiOS)

- **Guest bring-up**: Debian 13 trixie via debootstrap minbase, direct QEMU
  `-kernel`/`-initrd` boot with no bootloader, autologin as the `musashi`
  user on tty1.
- **Real webcam**: USB passthrough (XIFT Web Camera, `6211:e904`) → OpenCV →
  MediaPipe Hands (Tasks API) running inside the guest, ~15fps.
- **Desktop**: Phosh (Android-style Wayland shell) on top of the phoc
  compositor. Lock screen disabled via dconf (no keyboard, can't type a
  PIN).
- **Real touchscreen**: `gesture-engine/musashi_gestures/injector.py`
  creates a uinput device classified as a genuine touchscreen
  (`INPUT_PROP_DIRECT` + multi-touch protocol B), plus a second
  pointer-only device (visible aim cursor, never clicks) — see
  [docs/GESTURES.md](docs/GESTURES.md) and
  [docs/TOUCH-REDESIGN.md](docs/TOUCH-REDESIGN.md) for the decision history
  (written under the old name `haiku_gestures` — current code is already
  `musashi_gestures`).
- **Gestures → touch**: index+thumb pinch (tap/drag), middle+thumb pinch
  (long-press), open palm + fast move (edge swipe: home/notifications/app
  grid pages). Self-timed sequences (`sequencer.py`) run on a thread
  separate from the capture loop.
- **Fullscreen VM**: `run.sh` boots fullscreen 1920x1080 by default, with
  `--windowed`/`--vnc`/`--headless` as alternatives.
- **Protocol tests** (`gesture-engine/tests/test_injector_frames.py`) and
  real device-classification checks (`ID_INPUT_TOUCHSCREEN`/`ID_INPUT_MOUSE`
  via `udevadm`) running on the host, no VM needed.
- **HaikuOS → MusashiOS rename** (2026-08-19): guest user, Python package
  (`musashi_gestures`), paths (`/opt/musashi`, `/etc/musashi`), image
  (`musashi.qcow2`/`musashi.raw`), hostname, autologin, autostart
  `.desktop`, uinput device names. **Validated with a full build + headless
  boot** (2026-08-19): hostname `musashi`, autologin as `musashi` on tty1
  and ttyS0, working SSH (`ssh -p 2222 musashi@localhost`),
  `/opt/musashi/gesture-engine` and `/etc/musashi/config.toml` in the right
  place, `musashi_gestures` importable in the venv and loading config from
  the new paths, correct autostart `.desktop`, active `seat0` session via
  `loginctl`. The smoke test was headless with `--no-cam` — it doesn't
  cover the gesture experience with a real camera, which was validated
  below.

## Fixed — swipe gestures barely ever fired

On 2026-08-19, edge swipes (open palm + fast move) almost never triggered.
The cause wasn't threshold calibration — it was logic bugs:

- A live touch contact could get stuck pressed while a synthesized swipe
  used a different uinput slot, making phoc read two simultaneous contacts
  instead of a clean single-finger edge gesture.
- The wrist-velocity history was cleared on exactly the frame where motion
  blur makes MediaPipe lose the open-palm posture — i.e., at the peak
  velocity of the flick itself.
- Velocity was computed from position divided by hand scale instead of
  delta divided by hand scale, turning scale drift (distance to camera)
  into phantom velocity.
- Mirroring (`mirror_x`) was applied to the cursor but not to swipe
  direction detection, and the camera's 640×480 aspect ratio silently
  biased the axis-dominance test toward vertical.

See `docs/GESTURES.md` § Swipe detection for the fix. A real rejection-reason
log (`/tmp/gesture-engine.log` — previously documented but never actually
written) now exists, with three layers (per-frame, per-attempt episode,
5-second counters) so it's possible to tell "didn't fire" (engine) apart from
"fired but phoc ignored it" (compositor). 11 new host-side tests
(`gesture-engine/tests/test_gestures_swipe.py`) cover the fixed logic with
synthetic landmarks, no camera needed.

**Validated on the VM with a real webcam** (2026-08-19): 8 swipes fired
successfully in a short live session (`up` ×5, `down` ×3), all comfortably
above threshold, rejection reasons behaving sanely (`too-short`, `diagonal`,
`cooldown`), no errors, clean shutdown. Still open: whether phoc visually
responds to every fired swipe (home/notifications/app-grid paging) or only
registers some of them — see the open item below.

## Now — needs further VM validation

- Confirm that `phoc.ini` uses the right output name (`Virtual-1`) — if phoc
  enumerates a different name, the section is silently ignored and
  `scale=2` doesn't apply (QEMU fullscreen still works, only touch-target
  sizing is wrong). Check with `wlr-randr` or the phoc log.
- Confirm phoc recognizes the fired swipes as genuine edge gestures (origin
  close enough to the edge, plausible velocity) and not as a slow drag —
  use the log to tell "didn't fire" (engine) apart from "fired but phoc
  ignored it" (compositor).

## Later — pivot to MusashiOS (see Plano Diretor for the full roadmap)

The project's target is changing: from "gesture-controlled OS in a VM" to a
**voice-driven personal multi-LLM mainframe**, serving thin surfaces over the
network. Phase summary (full detail, budget, and verification in the Plano
Diretor):

- **S0–S2 (Phase 0 — Foundation)**: split the image into `musashi-core` and
  `musashi-surface`; PREEMPT_RT kernel; privacy state/classification
  skeleton (`水 SUI`) **before** any LLM; local inference (`火 KA`).
- **S3–S6 (Phase 1 — voice loop)**: capture + AEC, streaming STT/TTS, first
  web surface, room arbitration across multiple devices.
- **S7–S10 (Phase 2 — multi-LLM router)**: default-deny privacy policy,
  provider adapters (Claude API and others), typed tool calling, **red-team
  the system itself** (mandatory gate before real data).
- **S11–S14 (Phase 3 — surfaces and daily use)**: remote desktop surface,
  memory/context, `gesture-engine` repurposed as one surface's driver,
  degraded mode with the core offline. Project success criterion: two weeks
  of real use without reverting to the old workflow.
- **空 KŪ track (parallel, non-blocking)**: acoustic-interface R&D —
  levitation (TinyLev → Ultraino → SonicSurface) and mid-air haptics
  (GS-PAT), with its own go/no-go before any attempt at a volumetric
  display.

Items from the old scope that haven't started yet and remain valid as future
work within Phase 0/3 of the pivot:

- **Persistent gesture calibration** (hand size, distance to camera) per
  user — today only via manual `/etc/musashi/config.toml` edits.
- **Bare-metal / GRUB boot** — the current MVP only works with QEMU's
  `-kernel`/`-initrd`. Running outside a VM would need GRUB + real
  kernel/initrd on disk.
- **Multi-hand / two-hand gestures** — MediaPipe runs with `num_hands=1`.
- **Visual gesture-state feedback** — no overlay enabled by default (needs
  Xwayland, which the guest doesn't have); debugging today is log-only
  (`/tmp/gesture-engine.log`).

## Architecture notes worth remembering

- The compositor is **Phosh/phoc**, not labwc (swapped at some point before
  this session).
- uinput device classification in udev/libinput is **by declared
  capability, not by name or by emitted events** — a device with axes but no
  `EV_KEY` at all doesn't become `ID_INPUT_MOUSE`. See
  [docs/TOUCH-REDESIGN.md](docs/TOUCH-REDESIGN.md) for that detail (found by
  testing on the host, not documented anywhere obvious in the evdev API).
- Nothing in the pivot requires a new kernel — the base remains a
  verticalized Debian distro. See section 2.1 of the Plano Diretor for the
  full rationale behind that decision.
