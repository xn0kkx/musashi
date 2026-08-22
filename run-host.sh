#!/usr/bin/env bash
# Run the MusashiOS voice assistant directly on the host — no VM.
#
# Starts musashi-effector (host profile, shell.exec enabled) on a unix socket,
# then the always-on wake-word voice loop against it. Ctrl-C stops both.
#
# Prereqs (see docs/host-dev/voice.toml):
#   sudo apt install espeak-ng
#   ollama pull huihui_ai/qwen3-abliterated:30b-a3b
#   python3 -m venv .venv && . .venv/bin/activate
#   pip install -e gesture-engine/ -e effector/ -e 'voice/[audio,llm]'
#
# Usage:
#   ./run-host.sh                 # always-on, say "musashi ..." to trigger
#   ./run-host.sh --text "musashi, que horas são"   # one shot, no mic
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SOCK="${MUSASHI_EFF_SOCK:-/tmp/eff.sock}"
EFF_CFG="${MUSASHI_EFF_CONFIG:-$HERE/docs/host-dev/effector.toml}"
PY="${PYTHON:-python}"

# Prefer the project venv if present.
if [[ -x "$HERE/.venv/bin/python" ]]; then
    PY="$HERE/.venv/bin/python"
fi

cleanup() { [[ -n "${EFF_PID:-}" ]] && kill "$EFF_PID" 2>/dev/null || true; rm -f "$SOCK"; }
trap cleanup EXIT INT TERM

echo "starting musashi-effector (shell.exec enabled) on $SOCK ..."
"$PY" -m musashi_effector --unix "$SOCK" --config "$EFF_CFG" &
EFF_PID=$!
sleep 1.5

# The host profile lives in ~/.config/musashi/voice.toml; enable the LLM here
# too so it works even before that file is populated.
export MUSASHI_VOICE_VSOCK_UNIX_PATH="$SOCK"
export MUSASHI_VOICE_LLM_ENABLED=true

if [[ "${1:-}" == "--text" ]]; then
    "$PY" -m musashi_voice --unix "$SOCK" --text "${2:?usage: run-host.sh --text PHRASE}"
else
    echo 'ready — say "musashi ..." (Ctrl-C to quit)'
    "$PY" -m musashi_voice --unix "$SOCK" --wake "$@"
fi
