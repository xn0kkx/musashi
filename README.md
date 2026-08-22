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

The guest image, gesture input pipeline, intent/effector layer, a voice
MVP, and a first **host-side LLM voice assistant** are implemented and
validated (see [ROADMAP.md](ROADMAP.md) for the milestone log). This repo
started as a gesture-controlled OS prototype (old name: `HaikuOS`); on
2026-08-19 the project's target was redefined into the mainframe described
above, and the `haiku*` → `musashi*` rename was completed and validated. The
voice MVP is a proof of concept, not the Plano Diretor's S3–S6 voice loop:
`musashi-voice.service` now runs always-on, woken by the word "musashi" spotted
via `faster-whisper` (no dedicated wake-word model, no gesture trigger, no AEC,
no streaming, no web surface) — see [Voice](#voice) for the security trade-off
this accepted. On top of it, a **prototype LLM assistant** (an uncensored local
model with terminal and web access, spoken back with a natural Kokoro voice)
now answers whatever the command grammar misses — a first taste of the Plano
Diretor's S7–S10 multi-LLM router, validated **directly on the host with no
VM**; see [Assistant](#assistant-llm). The core/inference-node/surface split
and the privacy classifier (§0–§1 of the pivot) haven't started, and the
assistant has **no guard-rails yet** — what's here today is the pre-pivot base,
a voice proof of concept, and an agentic-LLM prototype, all feeding into
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
| `effector/` | `musashi_effector`: the capability daemon. `server.py` serves the intent protocol over `AF_VSOCK` **and** `AF_UNIX` simultaneously (one registry, one shared dispatch lock); `registry.py` is the only externally reachable tool table (`ui.tap`, `shell.swipe`, `app.launch`, `app.close`, and — host profile only — `shell.exec`); `apps.py` launches `.desktop` apps via `Gio.DesktopAppInfo`, tracking PIDs so `app.close` is deterministic; `shell.py` runs a shell command for the LLM agent (off unless `[shell].enabled`). |
| `voice/` | `musashi_voice`: the voice loop — capture + Silero VAD, `faster-whisper` STT, a fuzzy-match grammar built at runtime from the effector's own tool table, Piper TTS. Runs either inside the guest (mic/speaker passed through by QEMU, effector reached over a local unix socket) or on a host (effector reached over `vsock`) — see [Voice](#voice). Also holds the host LLM assistant: `assistant.py` (Ollama agentic tool-call loop), `webtools.py` (`web.search`/`web.fetch`), `tts_kokoro.py` (natural Kokoro voice) — see [Assistant](#assistant-llm). |
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
now ships **enabled** too, running always-on with wake-word activation — see
[Voice](#voice) for the security trade-off that accepts: without a real
second, non-audio factor, any audio near the microphone (including a
recording) can dispatch a command.

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
(`-audiodev pipewire` + `hda-duplex`; disable with `--no-audio`).

`musashi-voice.service` now runs **always-on inside the guest**, started
with the VM: capture never stops, Silero VAD segments speech by silence, and
each segment is transcribed with `faster-whisper` (`small`, CPU, baked into
the image at `/opt/musashi/whisper/small`) unconditionally. What used to
gate every transcription — a held button — is now a wake-word check: only a
transcript that *starts* with something phonetically close to "musashi" (a
fuzzy match against the word itself, `voice/musashi_voice/wakeword.py` —
there is no dedicated wake-word ML model, see the module docstring for why)
is resolved against the fuzzy-match command grammar built at runtime from
the effector's own `sys.tools` table (so a new allowlisted app is
voice-addressable with no code change), dispatched to `musashi-effector`
over the local unix socket, and answered with Piper
(`pt_BR-faber-medium`, baked in at `/opt/musashi/piper/`). Anything that
doesn't start with the wake word is discarded silently — logged at DEBUG,
nothing spoken back.

**This is a deliberate, accepted security trade-off, not an oversight.**
Voice input has no built-in authentication: Plano Diretor §2.7 calls for a
second, non-audio factor on `EFFECT` actions specifically because any sound
near the microphone — a person, a TV, a recording played back — can
otherwise trigger a real action. The previous design used push-to-talk (a
held button) as that second factor, and shipped `musashi-voice.service`
**disabled** rather than fake an always-on trigger. This change replaces
that button with the wake word above, at the cost of the §2.7 property: an
audio source that knows to say "musashi" can now dispatch a command with
nothing held down. See `build/overlay/etc/systemd/system/musashi-voice.service`
for the full trade-off writeup and what would close the gap (a confirmation
gate on destructive `EFFECT` tools — not implemented). The push-to-talk
harness is still there for manual, deliberate use, unchanged and still the
CLI default with no flags:

```sh
ssh -p 2222 musashi@localhost
/opt/gesture-engine/venv/bin/python -m musashi_voice -v          # PTT, manual
/opt/gesture-engine/venv/bin/python -m musashi_voice --wake -v   # what the service runs
```

Validated end-to-end on real hardware (2026-08-19, PTT MVP): "abrir a
calculadora" / "fechar a calculadora" launched and closed `gnome-calculator`
inside the guest for real, with a spoken Portuguese confirmation played back
through the host's speakers. Steady-state intent dispatch over the unix
socket: ~50ms. `musashi_voice` also runs on a host (unchanged from the
original design) talking to the guest over `vsock` instead — see
[voice/README.md](voice/README.md) for both arrangements, model config, and
the `--text`/`--list-tools`/`--devices`/`--wake` flags useful for testing.

`--wake` was validated the next day (2026-08-20) against a real cold boot of
the rebuilt image, not just a foreground run: `musashi-voice.service` starts
itself, connects to `musashi-effector`, loads Whisper, and reaches "ready
(wake word 'musashi')" with no manual step. Getting there surfaced two real
bugs neither the unit suite nor a foreground `python -m musashi_voice --wake`
run had caught — a CUDA-linked `torchaudio` wheel breaking the VAD on this
GPU-less guest, and a systemd ordering cycle that silently dropped the
unit's boot-time start job — both fixed; see
[ROADMAP.md](ROADMAP.md#done--always-on-wake-word-service-enabled-2026-08-20)
for the root causes. End-to-end dispatch was then re-validated with
Piper-synthesized speech fed through the real STT → wake → grammar →
effector chain: "musashi, abrir o terminal" opened `foot` inside the guest
for real, and speech without the wake word ("abrir o terminal" on its own)
was correctly discarded with no dispatch. Live validation with an actual
human voice near the host microphone is still outstanding.

## Assistant (LLM)

A prototype of the Plano Diretor's S7–S10 multi-LLM router, running **directly
on the host, no VM** — the fast way to iterate before the real
core/inference-node split exists. When the command grammar misses (anything
that is not one of the allowlisted `app.launch`/`shell.swipe` phrases), the raw
transcript is handed to a local **uncensored** LLM that can drive the terminal
and search the web, and answers in a natural spoken voice.

This is a **second seam**, parallel to and independent of the single-shot
`resolve_intent(..., fallback)` (which is left exactly as it was — an agentic,
multi-turn loop does not fit its `Intent | None` shape). It lives in
`voice/musashi_voice/assistant.py`:

- **`OllamaClient`** streams `/api/chat`; **`LlmAssistant`** runs a multi-turn
  tool-call loop. Its tool definitions are derived from the effector's live
  `sys.tools` table **plus** the web tools — one source, never a divergent copy.
- **The proposer never validates.** `shell.exec` and `app.*` are *proposed* to
  `musashi-effector`, which owns the schema, the allowlist, and the class gate
  and decides — the same contract gestures and the grammar use.
  `web.search`/`web.fetch` (`webtools.py`) run in-process because QUERY has no
  side effects to guard.
- **Natural voice.** `tts_kokoro.py` speaks with Kokoro-82M (via `kokoro-onnx`,
  Python-3.13-compatible), sentence-streamed so a long answer starts speaking
  while the model is still generating. Selected with `[tts].engine = "kokoro"`;
  Piper stays the default confirmation voice.

Run it on the host — the effector (with `shell.exec` enabled) and the wake-word
loop, one command:

```sh
sudo apt install espeak-ng                            # Kokoro's PT G2P
ollama pull huihui_ai/qwen3-abliterated:8b            # the default model
python3 -m venv .venv && . .venv/bin/activate
pip install -e gesture-engine/ -e effector/ -e 'voice/[audio,llm]'
# Kokoro model files (kokoro-onnx, not the torch package):
mkdir -p /opt/musashi/kokoro && cd /opt/musashi/kokoro
B=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
curl -fLO $B/kokoro-v1.0.onnx && curl -fLO $B/voices-v1.0.bin && cd -

./run-host.sh                                         # say "musashi, ..."
./run-host.sh --text "musashi, qual a versão do kernel"   # no mic, one shot
```

Example host configs are in [`docs/host-dev/`](docs/host-dev/). The default
model is `huihui_ai/qwen3-abliterated:8b` — on a 4 GB GPU it is the balance of
speed (~6 s per warm turn, mostly GPU-resident) and reliable tool use; swap for
`:30b-a3b` (more capable, ~13–45 s, mostly CPU) or `:4b` (fastest, flaky) in
`~/.config/musashi/voice.toml`. The model is warmed at startup in parallel with
the Whisper load, so the first spoken query does not pay the cold-start cost.

**No guard-rails yet — this is deliberate and not safe to leave.** `shell.exec`
is an unrestricted terminal, reachable by anything the wake word activates, with
no second non-audio factor (Plano Diretor §2.7) and no confirmation on
destructive actions. The architectural hooks are marked in place —
`Registry.gate(intent, spec)` for a confirmation / prefix allowlist, and the
`[llm].system_prompt` for a persona/refusal layer — but the policy behind them
is future work. Keep this host profile off any machine where that matters.

## Layout

- `build/` — image build scripts + rootfs overlay
- `gesture-engine/` — the `musashi_gestures` Python package (MediaPipe → uinput)
- `effector/` — the `musashi_effector` capability daemon
- `voice/` — the `musashi_voice` package (STT/intent/TTS loop + host LLM
  assistant; runs in the guest or on a host)
- `run.sh` — boots the VM
- `run-host.sh` — runs the LLM voice assistant on the host, no VM (see
  [Assistant](#assistant-llm) and `docs/host-dev/`)
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
- [docs/host-dev/README.md](docs/host-dev/README.md) — running the LLM voice
  assistant on the host (no VM): setup, model choice, and the safety caveat.
