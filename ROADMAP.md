# Roadmap

MusashiOS project state (old name: HaikuOS). Updated 2026-08-20.

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

## Done — intent/effector capability layer (V0, 2026-08-19)

Before voice could propose anything, the gesture engine needed a real
dispatch contract instead of the `if act.swipe: ...` chain it had:

- **`gesture-engine/musashi_gestures/intents.py`**: `Intent(name, args,
  source, confidence)`, `Registry` with schema validation, and a mandatory
  binary tool class — `QUERY` (free) or `EFFECT` (allowlisted, typed,
  confirmation gate planned for later). `__main__.py`'s dispatch loop now
  translates `Actions` into `Intent`s and hands them to a registry instead
  of branching on them directly; gesture behavior is unchanged.
- **`effector/` (new package)**: `musashi-effector`, a daemon that owns the
  only reachable copy of the real actuators (`app.launch`/`app.close` via
  `Gio.DesktopAppInfo`, PID-tracked so `app.close` is deterministic;
  `shell.swipe`/`ui.tap` reusing the existing `TouchSequencer`/
  `TouchInjector`). It listens on **both** `AF_VSOCK` (guest CID 3, port
  5000) and a local `AF_UNIX` socket (`/run/musashi/effector.sock`)
  simultaneously, sharing one registry and one dispatch lock — proven by a
  dedicated test with a negative control
  (`effector/tests/test_dual_listener_lock.py`) showing two *independently*
  locked servers really do race, so the shared-lock fix is provably load
  bearing, not just plausible.
- The `app.launch`/`app.close` allowlist lives in
  `/etc/musashi/effector.toml`, a file `update-gesture-engine.sh`
  deliberately never overwrites (unlike `config.toml`) — a capability
  boundary shouldn't reset on a routine code-iteration sync.
- **Validated live** against a real rebuilt VM: `sys.tools` returns the
  schema table; `app.launch`/`app.close` open and close real GUI apps
  (`foot`, `gnome-calculator`) inside the guest; an app id outside the
  allowlist is rejected by the daemon, not the caller; malformed JSON on the
  wire doesn't kill the connection.

## Done — voice MVP (2026-08-19/20)

A working push-to-talk voice loop, end to end, validated on real hardware —
explicitly a proof of concept for the intent/effector contract, not the
Plano Diretor's S3–S6 production voice loop (no wake word, no AEC, no
streaming, no web surface; push-to-talk is a terminal keypress, not the
gesture it will eventually be).

- **`voice/` (new package)**: capture (`sounddevice`) + Silero VAD →
  `faster-whisper` STT → a fuzzy-match grammar (`rapidfuzz`) built **at
  runtime** from the effector's own `sys.tools` table (a newly allowlisted
  app becomes voice-addressable with no client-side change) → dispatch to
  `musashi-effector` → Piper TTS confirmation. Per-stage latency
  instrumentation from day one, per the Plano Diretor's 900ms budget.
  `resolve_intent()` already has the seam for a local-LLM fallback on
  grammar misses (`fallback: Callable[[str, Grammar], Intent | None]`,
  contract pinned by 5 tests) — not implemented yet, deliberately out of
  scope for this pass.
- **Two supported arrangements, one codebase**: originally host-side (mic +
  GPU on the host, effector reached over `vsock`); now **also** fully
  inside the guest (QEMU passes the host mic/speaker through as an
  `intel-hda` card via `-audiodev pipewire` + `hda-duplex`, Whisper `small`
  + Piper `pt_BR-faber-medium` run on the guest's CPU, effector reached
  over the local unix socket). Only `[vsock]`/`[audio]` config differs
  between the two; `effector/musashi_effector/server.py` serving both
  transports at once is what made adding the second arrangement free on
  the effector side.
- Guest image grew from a 6G to a 12G sparse raw image
  (`build/build-image.sh`) to fit the baked-in models — CPU-only `torch`
  (no CUDA wheels, there's no GPU passthrough), Whisper `small` snapshot at
  `/opt/musashi/whisper/small`, Piper voice at `/opt/musashi/piper/` — so
  the guest is voice-capable offline from first boot.
- `musashi-voice.service` ships **disabled**, on purpose: without a real
  push-to-talk gesture, any automatic activation would discard the
  non-audio-second-factor security property PTT exists to provide (Plano
  Diretor §2.7 — a command replayed from a speaker must never execute).
- **Validated live** on the rebuilt VM, real hardware, no mocks: "abrir a
  calculadora" / "fechar a calculadora" launched and closed
  `gnome-calculator` for real, with a spoken Portuguese confirmation played
  back through the host's actual speakers. Steady-state intent dispatch
  over the unix socket measured at ~50ms (the Plano Diretor's 900ms budget
  is for the whole turn, not just this hop). A cold-boot run showed ~1s
  dispatch and load average 2.7 on the 4-vCPU guest — MediaPipe's
  continuous 14fps hand tracking competes with STT/TTS for the same CPUs;
  worth remembering before reading any single latency number as steady
  state.

## Design note — LLM inference moves off the core (Plano Diretor, 2026-08-19)

Not implemented in this repo yet — a Plano Diretor architecture revision
worth recording here because it changes what "S2 — 火 KA" means going
forward. The core no longer hosts the GPU or the local LLM directly: 火 KA
becomes a separate, stateless, directly-connected inference node (10GbE or
Thunderbolt, not the surfaces' LAN), reached by the core's router over an
OpenAI-compatible API (vLLM/llama.cpp). STT/TTS stay on the core (light,
latency-critical). Rationale and the updated topology, budget, and roadmap
entries are in the Plano Diretor §2.2/§2.3/§2.4/§4/§7.1 — the short version
is that a core carrying state *and* a GPU is a worse single point of
failure than a core carrying state alone; separating them turns a GPU
driver hang into an availability blip instead of a state-loss incident.

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
  skeleton (`水 SUI`) **before** any LLM; a separate inference node (`火
  KA`, see the design note above) reached over a direct link.
- **S3–S6 (Phase 1 — voice loop)**: capture + AEC, streaming STT/TTS, first
  web surface, room arbitration across multiple devices. **Not started** —
  the voice MVP above proves the intent/effector contract works, but it's a
  terminal-triggered, non-streaming, single-device proof of concept, not
  this phase.
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
