# Host dev profile — LLM voice assistant without a VM

Config and a launcher for running the MusashiOS voice assistant **directly on
the host** (no QEMU guest), the fast path for iterating on the LLM assistant
before the real core/inference-node split exists. See the repo's
[Assistant](../../README.md#assistant-llm) section for the design.

## Files

- **`effector.toml`** — layered onto the packaged effector config with
  `musashi-effector --config`. Enables `shell.exec` (an unrestricted terminal
  for the agent) **without** writing into `/etc/musashi`, and sets the host
  app allowlist. `../../run-host.sh` passes it automatically.
- **`voice.toml`** — the parts to copy into `~/.config/musashi/voice.toml`:
  reach the host effector over a unix socket, use the Kokoro voice, and enable
  the LLM. Merged section-by-section over the packaged defaults.

## Prerequisites

```sh
sudo apt install espeak-ng                        # Kokoro's PT-BR G2P
ollama pull huihui_ai/qwen3-abliterated:8b        # the default model
python3 -m venv .venv && . .venv/bin/activate
pip install -e gesture-engine/ -e effector/ -e 'voice/[audio,llm]'

# Kokoro model files — kokoro-onnx (NOT the torch `kokoro` package, which caps
# Python <3.13; host and guest are both 3.13):
mkdir -p /opt/musashi/kokoro && cd /opt/musashi/kokoro
B=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
curl -fLO $B/kokoro-v1.0.onnx && curl -fLO $B/voices-v1.0.bin
```

## Run

```sh
./run-host.sh                                     # always-on: say "musashi, ..."
./run-host.sh --text "musashi, que horas são"     # no mic, one shot
```

## Model choice (measured on i5-11400H / 32G / GTX 1650 4G)

| Model | Placement | Warm latency | Tool use |
|---|---|---|---|
| `:8b` (default) | mostly GPU | ~6 s/turn | reliable |
| `:30b-a3b` | 83% CPU (19G) | 13–45 s | most capable |
| `:4b` | all GPU | 2–4 s | flaky multi-step |

Set the model and `temperature` in `~/.config/musashi/voice.toml` under
`[llm]`. STT (`faster-whisper`) falls back to CPU if the NVIDIA driver is too
old for the installed torch's CUDA build (~5 s/transcription); use
`[stt].model = "small"` to speed it up, or update the driver.

## Safety

**There are no guard-rails yet.** `shell.exec` is unrestricted and reachable by
anything the wake word activates — no second non-audio factor, no confirmation
on destructive actions. Do not run this profile on a machine where that
matters. The hooks for guard-rails (`Registry.gate`, the `[llm].system_prompt`)
are in place; the policy is future work.
