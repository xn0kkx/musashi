# MusashiOS

MusashiOS is a personal, voice-driven, multi-LLM mainframe: a core machine
that concentrates state and context, a dedicated inference node that runs the
LLMs, and thin, stateless surfaces (screens, tablets, laptops) served over
the network. This repository holds the base it's built on: an isolated
Debian guest, run in QEMU/KVM, with a hand-gesture input pipeline (webcam →
touchscreen), a capability daemon that validates every action before it
runs, and now a working voice loop (speech → intent → action → spoken
confirmation) inside the guest. The full mainframe architecture — the
core/inference-node/surface split, the multi-LLM router, the privacy
classifier — is designed but not yet built; see [Status](#status).

## Status

The guest image, gesture input pipeline, intent/effector layer, and a voice
MVP are implemented and validated (see [ROADMAP.md](ROADMAP.md) for the
milestone log). This repo started as a gesture-controlled OS prototype (old
name: `HaikuOS`); on 2026-08-19 the project's target was redefined into the
mainframe described above, and the `haiku*` → `musashi*` rename was
completed and validated. The voice MVP is a proof of concept, not the
Plano Diretor's S3–S6 voice loop: push-to-talk is a terminal keypress (no
gesture trigger yet), there's no wake word, no AEC, no streaming, and no web
surface. The core/inference-node/surface split, the privacy classifier, and
the multi-LLM router (§0–§2 of the pivot) haven't started — what's here today
is the pre-pivot base plus a voice proof of concept, both feeding into
`musashi-core`'s foundation.

Full architecture and sprint plan: the Plano Diretor (Obsidian,
`1-Projects/MusashiOS/`) — updated 2026-08-19 to move LLM inference off the
core onto a separate, stateless, directly-connected inference node (§2.2).
Concrete repo state: [ROADMAP.md](ROADMAP.md).

## Architecture

```
                         ┌───────────────────────────────────┐
                         │        MUSASHI CORE (地/水)        │
                         │  state, index, memory, policy      │
                         │  STT/TTS (light, latency-critical)  │
                         │  multi-LLM router (decides, doesn't │
                         │  execute)                            │
                         │  no primary display of its own      │
                         └───┬───────────────────┬─────────────┘
                             │ direct link         │ 風 FŪ — mTLS, LAN
                             │ 10GbE/Thunderbolt    │
                    ┌────────▼────────┐   ┌────────┼──────────────────────┐
                    │ INFERENCE NODE  │   │        │                      │
                    │ (火) — GPU: LLM │   ┌────────▼───────┐  ┌───────────▼────────┐
                    │ + embeddings.   │   │ SURFACE: Desk  │  │ SURFACE: Tablet    │
                    │ Stateless,      │   │ multi-monitor  │  │ web UI, touch      │
                    │ replaceable.    │   │ + gesture-eng  │  │ zero-install       │
                    └─────────────────┘   └────────────────┘  └─────────────────────┘
```

Surfaces hold no state — a surface that dies loses nothing, since context
lives in the core. Neither does the inference node — the core sends context
with every request, the inference node forgets as soon as it answers, so
losing it mid-conversation is an availability problem, not a data-loss one.
The system is organized into five planes, named after the rings of the
*Go Rin no Sho*:

| Plane | Responsibility |
|---|---|
| 地 CHI (Earth) | Base: distro, kernel, boot, systemd, device PKI |
| 水 SUI (Water) | State and context: session memory, sensitivity classification, egress policy |
| 火 KA (Fire) | Inference: local LLMs, embeddings — its own machine, reached over a direct link |
| 風 FŪ (Wind) | Transport and surfaces: network, remote sessions, thin clients, room arbitration, and the core↔inference-node link |
| 空 KŪ (Void) | Parallel acoustic-interface R&D — non-blocking, own go/no-go |

**This repo today** is the 地 CHI base, one surface's input driver, and a
voice proof of concept — none of it is the core, the inference node, or a
real surface yet. The gesture pipeline isn't the center of the system, it's
how one future surface (the "surface desk") takes input; the voice loop
today runs self-contained inside the guest as a stand-in for the eventual
core, to prove the intent/effector contract end-to-end before the real
core/inference-node split exists.

## Components

| Path | What it is |
|---|---|
| `gesture-engine/` | `musashi_gestures`: MediaPipe Hands → `/dev/uinput` touchscreen. `camera.py`/`hands.py` capture and landmark detection, `gestures.py`/`sequencer.py` the gesture state machine, `injector.py` the uinput device, `intents.py` the shared Intent/Registry contract (see below). |
| `effector/` | `musashi_effector`: the capability daemon. `server.py` serves the intent protocol over `AF_VSOCK` **and** `AF_UNIX` simultaneously (one registry, one shared dispatch lock); `registry.py` is the only externally reachable tool table (`ui.tap`, `shell.swipe`, `app.launch`, `app.close`); `apps.py` launches `.desktop` apps via `Gio.DesktopAppInfo`, tracking PIDs so `app.close` is deterministic. |
| `voice/` | `musashi_voice`: the voice loop — capture + Silero VAD, `faster-whisper` STT, a fuzzy-match grammar built at runtime from the effector's own tool table, Piper TTS. Runs either inside the guest (mic/speaker passed through by QEMU, effector reached over a local unix socket) or on a host (effector reached over `vsock`) — see [Voice](#voice). |
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

The effector daemon listens on **both** `AF_VSOCK` (guest CID `3`, port
`5000` by default — see `VSOCK_CID` in `run.sh` and `[server]` in
`effector.toml`) and a local `AF_UNIX` socket (`/run/musashi/effector.sock`,
`[server].unix_path`) at the same time, sharing one registry and one dispatch
lock. Vsock is for a client outside the VM (the host, during development, or
eventually another surface); the unix socket is for `musashi-voice` running
inside the same guest. Either way it speaks JSON-lines, one connection per
client, any number of request/response pairs:

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
preview window); `gesture-engine.service` does not. `musashi-voice.service`
ships **disabled** — see [Voice](#voice) for why an automatic activation
would be a security regression, not a convenience, until a real push-to-talk
trigger exists.

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
./run.sh                # fullscreen 1920x1080, with webcam + audio
./run.sh --no-cam       # without webcam
./run.sh --no-audio     # without mic/speaker passthrough
./run.sh --windowed     # resizable GTK window instead of fullscreen (debug)
./run.sh --vnc          # no local display, exposes VNC on :1 — isolated inspection
./run.sh --headless     # no display at all, serial console only
```

The VM boots, autologs in as the `musashi` user on tty1, and brings up Phosh
(Android-style home screen), the gesture engine, and `musashi-effector`.
SSH is available at `ssh -p 2222 musashi@localhost` (password `musashi`).

Different webcam: `WEBCAM_VID=0x1234 WEBCAM_PID=0xabcd ./run.sh` (see
`lsusb`). Different guest vsock CID: `VSOCK_CID=4 ./run.sh` (needs the
`vhost_vsock` kernel module loaded and `rw` access to `/dev/vhost-vsock` —
grant it with `sudo setfacl -m u:$USER:rw /dev/vhost-vsock` if QEMU isn't
run as root). Different QEMU audio backend: `AUDIO_BACKEND=pa ./run.sh`
(default `pipewire`, which talks to the host's own user PipeWire session —
no extra device permissions needed for audio specifically).

## Tests

All test suites run on the host — no VM, no camera, no `/dev/uinput` needed:

```sh
python -m pytest gesture-engine/tests effector/tests voice/tests
```

Covers the gesture state machine (synthetic hand landmarks), the uinput
frame protocol and real device classification (`ID_INPUT_TOUCHSCREEN` /
`ID_INPUT_MOUSE` via `udevadm`), the Intent/Registry dispatch and validation
logic (including that the vsock and unix listeners genuinely share one
dispatch lock — see `effector/tests/test_dual_listener_lock.py`), and the
voice pipeline's vsock/unix protocol handling, fuzzy grammar matching, and
`resolve_intent` fallback contract. `voice/tests` needs only the package's
core install (`pip install -e voice/`), not the `[audio]` extra.

## Gestures

See [docs/GESTURES.md](docs/GESTURES.md) for the full hand-gesture → touch
semantics. Fine-tuning lives in `/etc/musashi/config.toml` inside the guest
(pinch thresholds, smoothing, active camera region).

## Voice

A working proof of concept, not the Plano Diretor's production voice loop —
see [Status](#status) for what's missing. `run.sh` passes the host's
microphone and speaker into the guest as an `intel-hda` card
(`-audiodev pipewire` + `hda-duplex`; disable with `--no-audio`). Inside the
guest, `musashi-voice` runs push-to-talk over a terminal (Enter to record,
Enter to stop — no gesture trigger exists yet, deliberately: see the "why
not a fake activation" note in `musashi-voice.service`), transcribes with
`faster-whisper` (`small`, CPU, baked into the image at
`/opt/musashi/whisper/small`), resolves the transcript against a fuzzy-match
grammar built at runtime from the effector's own `sys.tools` table (so a new
allowlisted app is voice-addressable with no code change), sends the intent
to `musashi-effector` over the local unix socket, and speaks the result back
with Piper (`pt_BR-faber-medium`, baked in at `/opt/musashi/piper/`):

```sh
ssh -p 2222 musashi@localhost
/opt/gesture-engine/venv/bin/python -m musashi_voice -v
```

Validated end-to-end on real hardware (2026-08-19): "abrir a calculadora" /
"fechar a calculadora" launched and closed `gnome-calculator` inside the
guest for real, with a spoken Portuguese confirmation played back through
the host's speakers. Steady-state intent dispatch over the unix socket:
~50ms. `musashi_voice` also runs on a host (unchanged from the original
design) talking to the guest over `vsock` instead — see
[voice/README.md](voice/README.md) for both arrangements, model config, and
the `--text`/`--list-tools`/`--devices` flags useful for testing without a
microphone.

## Layout

- `build/` — image build scripts + rootfs overlay
- `gesture-engine/` — the `musashi_gestures` Python package (MediaPipe → uinput)
- `effector/` — the `musashi_effector` capability daemon
- `voice/` — the `musashi_voice` package (STT/intent/TTS loop; runs in the
  guest or on a host)
- `run.sh` — boots the VM
- `out/` — generated artifacts (image, kernel, initrd); **not versioned**
  (`.gitignore`) — regenerate with `build/build-image.sh`
- `logs/` — local boot/build logs; **not versioned**
- `docs/` — technical decision history and gesture semantics

In-guest paths: sources under `/opt/musashi/{gesture-engine,effector,voice}`,
shared venv at `/opt/gesture-engine/venv` (name predates the rename, and now
also holds `musashi_effector` and `musashi_voice`), config at
`/etc/musashi/config.toml`, `/etc/musashi/effector.toml`, and
`/etc/musashi/voice.toml`, models baked in at `/opt/musashi/whisper/small`
and `/opt/musashi/piper/`, logs at `/tmp/gesture-engine.log` and
`/tmp/musashi-effector.log`.

## Further reading

- [ROADMAP.md](ROADMAP.md) — current state, milestone log, and the pivot's
  phase summary.
- [docs/GESTURES.md](docs/GESTURES.md) — hand-gesture → touch semantics.
- [docs/TOUCH-REDESIGN.md](docs/TOUCH-REDESIGN.md) — uinput device design
  decision history.
- [voice/README.md](voice/README.md) — the voice loop in detail: both
  arrangements (guest-local and host-over-vsock), model config, install,
  and testing without a microphone.
