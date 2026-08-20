# MusashiOS

MusashiOS is a personal, voice-driven, multi-LLM mainframe: a core machine
that concentrates inference, state, and context, and serves thin, stateless
surfaces (screens, tablets, laptops) over the network. This repository holds
the base it's built on: an isolated Debian guest, run in QEMU/KVM, with a
hand-gesture input pipeline (webcam → touchscreen) and a capability daemon
that validates every action before it runs. The mainframe architecture itself
— the core/surface split, the multi-LLM router, the privacy classifier — is
designed but not yet built; see [Status](#status).

## Status

The guest image, gesture input pipeline, and intent/effector layer are
implemented and validated (see [ROADMAP.md](ROADMAP.md) for the milestone
log). This repo started as a gesture-controlled OS prototype (old name:
`HaikuOS`); on 2026-08-19 the project's target was redefined into the
mainframe described above, and the `haiku*` → `musashi*` rename was
completed and validated. The core/surface split, the privacy classifier, and
the multi-LLM router (§0–§2 of the pivot) haven't started — what's here today
is the pre-pivot base, ready to become `musashi-core`'s foundation.

Full architecture and sprint plan: the Plano Diretor (Obsidian,
`1-Projects/MusashiOS/`). Concrete repo state: [ROADMAP.md](ROADMAP.md).

## Architecture

```
                         ┌───────────────────────────────────┐
                         │         MUSASHI CORE (地/水/火)    │
                         │  local LLM, STT, TTS, embeddings   │
                         │  state, index, memory, policy      │
                         │  no primary display of its own     │
                         └──────────────┬──────────────────────┘
                                        │ 風 FŪ — mTLS, LAN
              ┌─────────────────────────┼──────────────────────────┐
              │                         │                          │
      ┌───────▼────────┐      ┌─────────▼────────┐      ┌──────────▼────────┐
      │ SURFACE: Desk  │      │ SURFACE: Tablet  │      │ SURFACE: Ambient  │
      │ multi-monitor  │      │ web UI, touch    │      │ mic + speaker     │
      │ + gesture-eng  │      │ zero-install     │      │ only (voice)      │
      └────────────────┘      └──────────────────┘      └───────────────────┘
```

Surfaces hold no state — a surface that dies loses nothing, since context
lives in the core. The system is organized into five planes, named after the
rings of the *Go Rin no Sho*:

| Plane | Responsibility |
|---|---|
| 地 CHI (Earth) | Base: distro, kernel, boot, systemd, device PKI |
| 水 SUI (Water) | State and context: session memory, sensitivity classification, egress policy |
| 火 KA (Fire) | Inference: local/remote LLMs, STT, TTS, embeddings |
| 風 FŪ (Wind) | Transport and surfaces: network, remote sessions, thin clients, room arbitration |
| 空 KŪ (Void) | Parallel acoustic-interface R&D — non-blocking, own go/no-go |

**This repo today** is the 地 CHI base plus one surface's input driver: the
gesture pipeline is not the center of the system, it's how one surface (the
"surface desk") takes input.

## Components

| Path | What it is |
|---|---|
| `gesture-engine/` | `musashi_gestures`: MediaPipe Hands → `/dev/uinput` touchscreen. `camera.py`/`hands.py` capture and landmark detection, `gestures.py`/`sequencer.py` the gesture state machine, `injector.py` the uinput device, `intents.py` the shared Intent/Registry contract (see below). |
| `effector/` | `musashi_effector`: the capability daemon. `server.py` serves the intent protocol over `AF_VSOCK` (or `AF_UNIX` for host-side testing); `registry.py` is the only externally reachable tool table (`ui.tap`, `shell.swipe`, `app.launch`, `app.close`); `apps.py` launches `.desktop` apps via `Gio.DesktopAppInfo`, tracking PIDs so `app.close` is deterministic. |
| `build/` | Image build: `debootstrap` + chroot config + rootfs overlay. |
| `docs/` | Technical decision history and gesture semantics. |

## Intent layer

`gesture-engine/musashi_gestures/intents.py` defines the contract every
proposer — gestures today, voice later — uses to request an action, and
`effector/musashi_effector/registry.py` is where that contract is enforced.
The rule is: **the proposer never validates.** A caller hands over a tool
name and a bag of args; the Registry owns the schema, the value allowlists,
and the class gate, and decides.

Every tool carries a mandatory, binary class — there is deliberately no
third:

- **QUERY** — reads state, no side effects, free to call.
- **EFFECT** — changes the world; allowlisted and typed (confirmation gate is
  planned for a later pass).

The effector daemon listens on `AF_VSOCK` (guest CID `3`, port `5000` by
default — see `VSOCK_CID` in `run.sh` and `[server]` in `effector.toml`) and
speaks JSON-lines, one connection per client, any number of request/response
pairs:

```
->  {"tool": "app.launch", "args": {"id": "foot.desktop"}}
<-  {"ok": true, "result": {"id": "foot.desktop", "pid": 812}, "error": null}

->  {"tool": "app.launch", "args": {"id": "/bin/sh"}}
<-  {"ok": false, "result": null, "error": "app.launch: 'id' value not allowed: '/bin/sh'"}
```

Reachable from the host with no VM network exposure required:
`socat - VSOCK-CONNECT:3:5000`.

The `app.launch`/`app.close` allowlist lives in
`/etc/musashi/effector.toml`, deliberately a separate file from
`/etc/musashi/config.toml`: `update-gesture-engine.sh` overwrites the latter
on every sync, so keeping the capability allowlist out of it means a routine
code-iteration cycle can never silently widen or reset it.

`musashi-effector.service` ships **enabled** (it needs no webcam and no
preview window); `gesture-engine.service` does not.

## Build

```sh
sudo ./build/build-image.sh    # debootstrap + chroot config + qcow2 (~15-45 min)
```

Produces `out/musashi.qcow2`, `out/vmlinuz`, and `out/initrd.img` (direct
QEMU boot, no bootloader).

For iterating on Python code without a full rebuild:

| Command | Rebuilds |
|---|---|
| `sudo ./build/update-gesture-engine.sh` | `gesture-engine` + `effector` packages only (~1 min) |
| `sudo ./build/update-gesture-engine.sh --deps` | + dependencies (mediapipe/evdev changed) |
| `sudo ./build/update-gesture-engine.sh --overlay` | + `build/overlay/` (phoc.ini, udev, dconf, systemd, autostart) |

Without `--overlay`, changes to `build/overlay/` require a full rebuild
(`build-image.sh`).

## Run

```sh
./run.sh                # fullscreen 1920x1080, with webcam (needs sudo for /dev/bus/usb)
./run.sh --no-cam       # without webcam
./run.sh --windowed     # resizable GTK window instead of fullscreen (debug)
./run.sh --vnc          # no local display, exposes VNC on :1 — isolated inspection
./run.sh --headless     # no display at all, serial console only
```

The VM boots, autologs in as the `musashi` user on tty1, and brings up Phosh
(Android-style home screen), the gesture engine, and `musashi-effector`.
SSH is available at `ssh -p 2222 musashi@localhost` (password `musashi`).

Different webcam: `WEBCAM_VID=0x1234 WEBCAM_PID=0xabcd ./run.sh` (see
`lsusb`). Different guest vsock CID: `VSOCK_CID=4 ./run.sh`.

## Tests

All test suites run on the host — no VM, no camera, no `/dev/uinput` needed:

```sh
python -m pytest gesture-engine/tests effector/tests
```

Covers the gesture state machine (synthetic hand landmarks), the uinput
frame protocol and real device classification (`ID_INPUT_TOUCHSCREEN` /
`ID_INPUT_MOUSE` via `udevadm`), and the Intent/Registry dispatch and
validation logic.

## Gestures

See [docs/GESTURES.md](docs/GESTURES.md) for the full hand-gesture → touch
semantics. Fine-tuning lives in `/etc/musashi/config.toml` inside the guest
(pinch thresholds, smoothing, active camera region).

## Layout

- `build/` — image build scripts + rootfs overlay
- `gesture-engine/` — the `musashi_gestures` Python package (MediaPipe → uinput)
- `effector/` — the `musashi_effector` capability daemon
- `run.sh` — boots the VM
- `out/` — generated artifacts (image, kernel, initrd); **not versioned**
  (`.gitignore`) — regenerate with `build/build-image.sh`
- `logs/` — local boot/build logs; **not versioned**
- `docs/` — technical decision history and gesture semantics

In-guest paths: sources under `/opt/musashi/{gesture-engine,effector}`,
shared venv at `/opt/gesture-engine/venv` (name predates the rename),
config at `/etc/musashi/config.toml` and `/etc/musashi/effector.toml`, logs
at `/tmp/gesture-engine.log` and `/tmp/musashi-effector.log`.

## Further reading

- [ROADMAP.md](ROADMAP.md) — current state, milestone log, and the pivot's
  phase summary.
- [docs/GESTURES.md](docs/GESTURES.md) — hand-gesture → touch semantics.
- [docs/TOUCH-REDESIGN.md](docs/TOUCH-REDESIGN.md) — uinput device design
  decision history.
