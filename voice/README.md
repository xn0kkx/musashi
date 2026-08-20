# musashi-voice — the voice loop

One package, **two places it runs**.

**In the guest** (the current target). The whole loop lives inside the VM:
QEMU passes the host's microphone and speaker through as an `intel-hda` card,
Whisper and Piper run on the guest's CPU, and the effector is a local process
reached over a unix socket. No VM boundary is crossed at all.

```
QEMU intel-hda ─► sounddevice/ALSA ─► Silero VAD ─► faster-whisper (CPU) ─┐
                                                    grammar+rapidfuzz  ───┤
    speaker ◄─ Piper TTS ◄─ reply ◄─ musashi-effector ◄── unix socket ◄───┘
                                     /run/musashi/effector.sock
```

**On the host** (still supported, unchanged). The original V1 arrangement: mic
and GPU on the host, effector across `virtio-vsock` at `cid=3:5000`.

```
mic ─► sounddevice ─► Silero VAD ─► faster-whisper ─► grammar+rapidfuzz ─┐
                                                                        │ vsock cid=3:5000
speaker ◄─ Piper TTS ◄──────────────── reply ◄── musashi-effector (guest) ◄┘
```

The code is identical; only config differs (`[vsock].unix_path`). The effector
serves **both transports simultaneously** over one registry and one dispatch
lock, so moving voice into the guest cost nothing on the host side — `socat -
VSOCK-CONNECT:3:5000` still works exactly as before.

Nothing here validates anything, in either arrangement. The caller *proposes*
an intent; the effector's `Registry` owns the allowlist and decides. Plano
Diretor §2.7: *"O LLM propõe; `chid` decide."*

## Install

```bash
python3 -m venv ~/.venvs/musashi-voice
source ~/.venvs/musashi-voice/bin/activate

# core only — enough for --text mode and the whole test suite
pip install -e voice/

# the real loop: capture, VAD, STT, TTS
pip install -e 'voice/[audio]'
```

CUDA is optional. `[stt].device = "auto"` tries `cuda` and falls back to `cpu`
on its own; nothing has to be configured for a laptop with no GPU.

## Models

Neither model is downloaded by this package **on the host**. In the guest both
are baked into the image by `build/chroot-setup.sh` (`/opt/musashi/whisper/small`
and `/opt/musashi/piper/`), so the VM is offline-capable from first boot; the
rest of this section is about a host checkout.

**Whisper** is fetched by `faster-whisper` from the Hugging Face hub on first
use and cached in `~/.cache/huggingface`. The default is `large-v3-turbo`. On a
CPU-only machine start smaller:

```bash
MUSASHI_VOICE_STT_MODEL=small python -m musashi_voice
```

**Piper voice** must be downloaded by hand — two files that live side by side:

```bash
mkdir -p ~/.local/share/piper
cd ~/.local/share/piper
BASE=https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium
curl -LO $BASE/pt_BR-faber-medium.onnx
curl -LO $BASE/pt_BR-faber-medium.onnx.json
```

Then point the config at the `.onnx` (the `.json` is found next to it):

```toml
# ~/.config/musashi/voice.toml
[tts]
model = "~/.local/share/piper/pt_BR-faber-medium.onnx"
```

With no voice configured the loop still runs; replies are printed as
`[voice] abrindo terminal` instead of spoken.

## Configure

Four layers, each overriding the one before:

1. packaged `musashi_voice/voice.toml` (documented defaults, host-flavoured)
2. `/etc/musashi/voice.toml` (section by section) — *this is the guest layer*
3. `~/.config/musashi/voice.toml` (section by section)
4. `MUSASHI_VOICE_<SECTION>_<KEY>` env vars (key by key)

Layer 2 is what makes one package serve both arrangements. It does not exist on
the host; inside the image it ships via `build/overlay/etc/musashi/voice.toml`
and is only rewritten by `update-gesture-engine.sh --overlay`, never by a plain
code-iteration run.

The knobs that matter:

| key | host default | guest (`/etc`) | note |
|---|---|---|---|
| `vsock.unix_path` | *(empty)* | `/run/musashi/effector.sock` | non-empty wins over cid/port |
| `vsock.cid` | `3` | *(unused)* | must match `run.sh`'s `-device vhost-vsock-pci,guest-cid=` |
| `vsock.port` | `5000` | *(unused)* | must match the effector's `[server].vsock_port` |
| `stt.model` | `large-v3-turbo` | `/opt/musashi/whisper/small` | a faster-whisper name or a local dir |
| `stt.device` | `auto` | `cpu` | `auto` \| `cuda` \| `cpu` |
| `stt.language` | `pt` | `pt` | `pt` \| `en` \| `auto` |
| `intent.min_score` | `75.0` | — | below this a transcript is a MISS |
| `tts.model` | *(empty)* | `/opt/musashi/piper/pt_BR-faber-medium.onnx` | path to the Piper `.onnx` |
| `wake.word` | `musashi` | same | `--wake` mode only; ignored by PTT |
| `wake.threshold` | `75.0` | same | fuzz.ratio cutoff, see `wakeword.py` |
| `wake.silence_ms` | `700` | same | biggest single knob on `--wake` latency |

`--cid`/`--port` on the command line force vsock even when `unix_path` is set,
so a guest shell can still be pointed at some other VM if that is ever useful.

## Run inside the guest

Nothing to install — `build/chroot-setup.sh` puts this package, a CPU-only
torch, the Whisper `small` weights and the Piper pt_BR voice into the image, and
`/etc/musashi/voice.toml` wires them together. Boot the VM and:

```bash
./run.sh                                  # host: mic + speaker now passed through
ssh -p 2222 musashi@localhost             # password: musashi

# prove the sound card arrived before blaming Python
arecord -l                                # should list one intel-hda card
arecord -d 3 -f S16_LE -r 16000 /tmp/t.wav && aplay /tmp/t.wav

/opt/gesture-engine/venv/bin/python -m musashi_voice
```

That runs the PTT harness in the foreground, for manual testing. In the
image itself, `musashi-voice.service` runs this package a different way —
`--wake`, always-on, started with the VM:

```bash
systemctl status musashi-voice
journalctl -fu musashi-voice
```

**This is not the same activation as PTT, and the difference matters.** The
unit used to ship disabled because an automatic activation discards the
security property push-to-talk exists to provide (Plano Diretor §2.7 — see
"Push-to-talk" below); it now ships **enabled**, running `--wake` instead,
because that trade-off was explicitly accepted rather than avoided. The unit
file has the full writeup. The PTT harness above still works exactly as
before and is the way to test manually without accepting that trade-off.

Useful there too:

```bash
/opt/gesture-engine/venv/bin/python -m musashi_voice --wake         # what the service runs
/opt/gesture-engine/venv/bin/python -m musashi_voice --list-tools
/opt/gesture-engine/venv/bin/python -m musashi_voice --text "abrir o terminal"
/opt/gesture-engine/venv/bin/python -m musashi_voice --devices
```

## Run on the host

```bash
# the real thing, against the VM
python -m musashi_voice

# no VM: run the guest daemon on the host over a unix socket
python -m musashi_effector --unix /tmp/eff.sock &
python -m musashi_voice --unix /tmp/eff.sock

# no mic, no GPU: resolve one phrase and act on it
python -m musashi_voice --unix /tmp/eff.sock --text "abrir o terminal"

# what is speakable right now, derived from the guest's live sys.tools
python -m musashi_voice --list-tools

python -m musashi_voice --devices     # pick an input device
python -m musashi_voice --no-tts -v   # print replies, debug logging
```

**Push-to-talk** (default mode, no flags): press Enter to start recording,
Enter again to stop. Originally a stand-in for a hand-gesture PTT that never
got built; it remains here, unchanged, as the manual/testing mode — every
flag and behavior described in this README for the no-`--wake` case still
applies exactly as before. It is the second, non-audio factor Plano Diretor
§2.7 asks for on `EFFECT` actions, and it neutralises the speaker-replay
attack class by construction: nothing is heard unless a human is holding the
key down for it.

**`--wake`** (what `musashi-voice.service` runs): no key, no gesture,
listening continuously. `musashi_voice/audio.py`'s `listen()` uses Silero VAD
to segment speech by silence automatically instead of waiting for `stop` to
be set, and every segment is transcribed unconditionally. What used to be
the PTT gate is now `musashi_voice/wakeword.py`'s `strip_wake_word()`: a
transcript is only acted on if it *starts* with something a fuzzy match
(`rapidfuzz.fuzz.ratio`, default threshold 75.0 — see that module's
docstring for the measured corpus behind the number) accepts as close enough
to "musashi". No dedicated wake-word model is used or trained — pretrained
openWakeWord models are English-only, and a PT-BR model needs a GPU training
pipeline out of scope here; reusing the STT that already runs was the
available alternative. **Accept that this drops the §2.7 property above**:
without a human holding a key, any audio able to say "musashi" near the
microphone can dispatch a command, including a recording. The `[wake]`
section of `voice.toml` tunes this mode (`word`, `threshold`, `silence_ms`,
`vad_threshold`, `preroll_ms`, `min_speech_ms`, `resume_delay_ms`) and does
not affect PTT mode at all.

Every interaction prints its latency by stage, clocked from the moment you stop
talking (PTT) or from the moment the VAD closes the utterance (`--wake`, where
`[wake].silence_ms` is therefore part of every WALL number below):

```
[utterance] latency by stage:
  vad          12.4 ms
  stt         310.7 ms
  intent        0.2 ms
  vsock         3.1 ms
  tts          88.0 ms
  MEASURED    414.4 ms  (budget 900 ms)
  WALL        415.9 ms
```

(`--wake` mode replaces the `vad` stage above with a `wake` stage — the
`strip_wake_word()` check — since the VAD's work already happened inside
`audio.listen()`, before the utterance ever reached `handle_wake_audio()`.)

## What it understands

The grammar is built at startup from whatever the guest answers to `sys.tools`
— nothing about the tool table is hardcoded here. Add a tool to the effector,
restart this process, and it is speakable.

| tool | pt-BR | en |
|---|---|---|
| `app.launch` | "abrir o terminal", "abre a calculadora" | "open the terminal", "launch calculator" |
| `app.close` | "fechar o terminal", "encerra a calculadora" | "close the calculator", "quit terminal" |
| `shell.swipe` | "deslizar para cima", "desliza para a direita" | "swipe left", "swipe down" |
| `sys.tools` | "listar comandos" | "what can you do" |

`ui.tap` is deliberately not speakable: its args are two floats with no
allowlist, and "tap at zero point three seven" is a dictation problem, not a
grammar problem.

Two tables in `grammar.py` are hand-written and meant to be edited:
`VERBS` (how a tool is said) and `ALIASES` (how an allowlisted value is said —
`foot.desktop` → "terminal"). A value missing from `ALIASES` is not mute: a
spoken form is derived from the id itself, so a newly allowlisted
`org.gnome.Weather.desktop` answers to "abrir weather" on day one.

A transcript that matches nothing is a **MISS**: it is logged and answered with
"não entendi". The LLM fallback the plan describes (llama.cpp + Qwen, typed
tools derived from the same `sys.tools` table) is **not** implemented in V1 —
what exists is its seam, `resolve_intent(transcript, grammar, fallback=...)`.

## Tests

No hardware, no VM, no torch:

```bash
python3 voice/tests/test_vsock_protocol.py
python3 voice/tests/test_grammar_match.py
python3 voice/tests/test_resolve_intent.py
python3 voice/tests/test_wake_word.py
python3 voice/tests/test_vad_segmenter.py
```

## Troubleshooting

**"could not read the tool table"** — the effector is not answering. The
message prints the target it tried, which tells you which half to check.

*On the host*: the VM must be up (`./run.sh`), `musashi-effector` running
inside it, and `vsock.cid` matching `run.sh`. To isolate the host side from the
VM entirely, run the daemon locally with `--unix` as shown above.

*In the guest*: `systemctl status musashi-effector`, then confirm the socket
exists — `ls -l /run/musashi/effector.sock`. It is created by the unit's
`RuntimeDirectory=musashi` and is mode 0660, owned by `musashi`; running the
loop as any other user will not reach it.

**No audio captured** — `python -m musashi_voice --devices`, then set
`[audio].input_device` to the device name or index.

**Whisper is slow** — check the startup log line `STT ready: <model> on
<device>`. On the host, if it says `cpu`, either the GPU is not visible to
CTranslate2 or CUDA libs are missing; drop to `MUSASHI_VOICE_STT_MODEL=small`
meanwhile. In the guest `cpu` is correct and expected — there is no GPU and
there will not be one. If `small` is too slow or the 4 GB VM starts swapping,
step down to `base` before `tiny`:

```bash
MUSASHI_VOICE_STT_MODEL=base /opt/gesture-engine/venv/bin/python -m musashi_voice
```

(A bare name re-downloads from Hugging Face into `~/.cache`; only the packaged
`/opt/musashi/whisper/small` works with no network.)

**No mic in the guest** — `arecord -l` lists nothing. Check the host side:
`./run.sh` must not have been given `--no-audio`, and QEMU needs a working
`pipewire` backend (`qemu-system-x86_64 -audiodev help`; `AUDIO_BACKEND=pa` is
the fallback). Note there is no PipeWire *daemon* inside the guest — this image
has no systemd user session — so PortAudio talks to ALSA directly. That is
intended, not a missing piece.
