#!/bin/bash
# Runs INSIDE the chroot: packages, user, services, gesture-engine venv.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update

# Core system
# libpam-systemd registers login sessions with systemd-logind, which sets
# XDG_RUNTIME_DIR — required for phoc (Wayland) to start at all.
apt-get install -y --no-install-recommends \
    linux-image-amd64 systemd-sysv dbus udev kmod ca-certificates sudo \
    openssh-server iproute2 libpam-systemd

# Graphics: Phosh (Android-like mobile shell) on phoc (wlroots compositor)
# xwayland: needed for musashi_gestures' cv2.imshow debug preview (overlay.py);
# also requires xwayland=true in overlay/etc/phosh/phoc.ini.
apt-get install -y --no-install-recommends \
    phosh phoc phosh-core gnome-session-bin gnome-session-common \
    dconf-gsettings-backend dconf-cli xdg-desktop-portal-phosh \
    libgl1-mesa-dri seatd foot gnome-calculator xwayland

# Camera + gesture engine runtime
apt-get install -y --no-install-recommends \
    v4l-utils python3 python3-venv \
    libgl1 libglib2.0-0 libsm6 libxext6 libxrender1

# Effector runtime: PyGObject for Gio.DesktopAppInfo (app.launch/app.close),
# socat for poking the vsock intent socket by hand from the host.
# wlrctl is NOT installed yet — it is only needed by the window.focus stub in
# musashi_effector/apps.py, which is blocked on confirming that phoc exposes
# wlr-foreign-toplevel-management. Add it here when that stub is implemented.
apt-get install -y --no-install-recommends \
    python3-gi gir1.2-glib-2.0 socat

# Voice runtime (STT + TTS now run INSIDE this VM, not on the host).
#
# QEMU gives the guest one intel-hda card (run.sh: -device intel-hda +
# hda-duplex). sounddevice reaches it through PortAudio -> ALSA *directly*:
# libportaudio2 is the runtime PortAudio (portaudio19-dev is build-time only
# and not needed, sounddevice ships a CFFI binding, not a C extension).
#
# PipeWire is installed because the plan asks for it and because it is the
# right long-term answer, but note it is NOT what makes audio work here: this
# image has no systemd *user* session (the session starts from musashi's login
# shell — see overlay/home/musashi/.bash_profile), so no PipeWire daemon is
# running and ALSA-direct is the path that actually carries the audio. If a
# user session ever appears, sounddevice picks up the pipewire/pulse device
# with no change to this image.
#
# libsndfile1 is pulled in by several audio libs; alsa-utils is here so
# `arecord -l` / `aplay /usr/share/sounds/...` can prove the card works from
# an SSH session before blaming the Python stack.
apt-get install -y --no-install-recommends \
    libportaudio2 libsndfile1 alsa-utils \
    pipewire pipewire-audio pipewire-pulse wireplumber \
    curl

# Build toolchain for evdev's C extension (needs linux/input.h + Python.h),
# only for the pip install below — removed afterwards to keep the image lean.
apt-get install -y --no-install-recommends \
    gcc libc6-dev python3-dev linux-headers-amd64

# User: video (camera/seatd), input (uinput), render (gpu)
useradd -m -s /bin/bash -G video,input,render,sudo musashi
echo 'musashi:musashi' | chpasswd

# gesture-engine + effector + voice share one venv (PEP 668: no system pip
# installs). --system-site-packages is required so python3-gi (a distro
# package, never a wheel) is importable; venv site-packages still takes
# precedence for everything pip installs here.
python3 -m venv --system-site-packages /opt/gesture-engine/venv
/opt/gesture-engine/venv/bin/pip install --no-cache-dir /opt/musashi/gesture-engine
/opt/gesture-engine/venv/bin/pip install --no-cache-dir --no-deps /opt/musashi/effector

# --- voice -------------------------------------------------------------
# torch AND torchaudio FIRST, both from PyTorch's CPU-only index. This is not
# an optimisation, it is the difference between ~200 MB of CPU wheels and
# ~2.5 GB of nvidia-* CUDA packages that this VM can never use: there is no
# GPU here and GPU passthrough was explicitly ruled out. Installing both up
# front means the dependency below is already satisfied and pip never
# reaches the default index for either.
#
# torchaudio matters even though nothing here plays audio through it:
# silero-vad's package unconditionally does `import torchaudio` at module
# load, and the *default* PyPI torchaudio wheel is linked against
# libcudart.so — which does not exist on a CPU-only guest and turns into a
# hard ImportError the moment --wake mode tries to load the VAD (torch alone
# being CPU-only is not enough; pip is happy to pair it with a CUDA
# torchaudio if that's what the default index serves for the dependency).
# Discovered by booting a real image and watching musashi-voice.service fail
# to start under --wake; PTT never surfaced it because trim_silence()
# degrades silently when the VAD fails to load.
#
# torch/torchaudio arrive via silero-vad, not faster-whisper — faster-whisper
# runs on CTranslate2 and has no torch dependency at all.
/opt/gesture-engine/venv/bin/pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu torch torchaudio

/opt/gesture-engine/venv/bin/pip install --no-cache-dir '/opt/musashi/voice[audio]'

# Prefetch the Whisper weights so the guest is usable offline on first boot —
# faster-whisper would otherwise download from Hugging Face at the first
# utterance, and this VM may have no network at all.
#
# Deliberately snapshot_download() into a fixed path rather than letting the
# HF cache fill: this script runs as root, but musashi-voice runs as musashi.
# A ~/.cache/huggingface populated here would land in /root and be invisible to
# the service. A path under /opt is world-readable and needs no HF_HOME.
# /etc/musashi/voice.toml [stt].model points at this directory.
/opt/gesture-engine/venv/bin/python - <<'EOF'
from huggingface_hub import snapshot_download
from faster_whisper import WhisperModel

path = snapshot_download("Systran/faster-whisper-small",
                         local_dir="/opt/musashi/whisper/small")
# Loading it here both validates the download and fails the build early, in
# the chroot, rather than at the first spoken word in a booted VM.
WhisperModel(path, device="cpu", compute_type="int8")
print(f"whisper small/int8 ready at {path}")
EOF

# Piper pt_BR voice: two files that must sit side by side (.onnx + .onnx.json).
# medium (~63 MB, 22.05 kHz) rather than low — TTS here speaks four-word
# confirmations a handful of times a minute, so its cost is noise next to
# Whisper's, and low quality is audibly worse for exactly the short phrases we
# use. /etc/musashi/voice.toml points [tts].model at this path.
mkdir -p /opt/musashi/piper
PIPER_BASE=https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium
curl -fsSL -o /opt/musashi/piper/pt_BR-faber-medium.onnx \
    "$PIPER_BASE/pt_BR-faber-medium.onnx"
curl -fsSL -o /opt/musashi/piper/pt_BR-faber-medium.onnx.json \
    "$PIPER_BASE/pt_BR-faber-medium.onnx.json"

systemctl enable seatd systemd-networkd ssh
# Unlike gesture-engine.service, the effector needs no webcam — ship it running.
systemctl enable musashi-effector
# Now always-on (wake word "musashi", not push-to-talk) — it has no console
# to be attached to, so it has to start itself. See the unit file for the
# §2.7 security trade-off this accepts.
systemctl enable musashi-voice

# No keyboard on this system — compile the dconf override that disables the
# Phosh lock screen's PIN/password prompt (see overlay/etc/dconf).
dconf update

# Session starts from musashi's login shell (phosh-session); gesture-engine
# autostarts via the XDG autostart entry (see overlay).
chown -R musashi:musashi /home/musashi

# Drop the build toolchain now that the venv wheels are already built.
apt-get purge -y gcc libc6-dev python3-dev linux-headers-amd64
apt-get autoremove -y

apt-get clean
rm -rf /var/lib/apt/lists/*
# Any Hugging Face blobs left over from the Whisper snapshot above. The weights
# themselves live in /opt/musashi/whisper and are not touched by this.
rm -rf /root/.cache/huggingface /root/.cache/pip
