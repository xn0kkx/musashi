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

# Build toolchain for evdev's C extension (needs linux/input.h + Python.h),
# only for the pip install below — removed afterwards to keep the image lean.
apt-get install -y --no-install-recommends \
    gcc libc6-dev python3-dev linux-headers-amd64

# User: video (camera/seatd), input (uinput), render (gpu)
useradd -m -s /bin/bash -G video,input,render,sudo musashi
echo 'musashi:musashi' | chpasswd

# gesture-engine in a dedicated venv (PEP 668: no system pip installs)
python3 -m venv /opt/gesture-engine/venv
/opt/gesture-engine/venv/bin/pip install --no-cache-dir /opt/musashi/gesture-engine

systemctl enable seatd systemd-networkd ssh

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
