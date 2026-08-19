# MusashiOS

A personal multi-LLM mainframe, voice-driven, serving thin surfaces (screens,
tablets, laptops) over the network. Full plan in `Plano-Diretor.md`
(Obsidian, `1-Projects/MusashiOS/`) — for now, this repo holds the base
inherited from the earlier prototype (old name: HaikuOS): an isolated Debian
guest in a QEMU/KVM VM, controlled by hand gestures via webcam, with no mouse
or keyboard.

```
HOST (QEMU/KVM)                     GUEST "musashi"
webcam USB ──passthrough──▶ /dev/video0 → OpenCV → MediaPipe Hands
                                     → gesture state machine
                                     → uinput: real touchscreen (aim cursor +
                                       tap/drag/long-press/edge swipes)
                                     → Phosh/phoc (Wayland, Android-style)
```

In the new architecture, this gesture pipeline becomes the input driver for
**one** surface (the "surface desk") — no longer the center of the system.
See [docs/GESTURES.md](docs/GESTURES.md) for the full hand-gesture → touch
semantics, and [ROADMAP.md](ROADMAP.md) for the project's current state and
the MusashiOS pivot roadmap.

## Build

```sh
sudo ./build/build-image.sh    # debootstrap + chroot config + qcow2 (~15-45 min)
```

Produces `out/musashi.qcow2`, `out/vmlinuz`, and `out/initrd.img` (direct
QEMU boot, no bootloader).

Only touched `gesture-engine/` (Python code)? No need to rebuild the whole
image — this mounts the existing qcow2 via NBD, syncs the package, and
reinstalls it into the venv:

```sh
sudo ./build/update-gesture-engine.sh            # ~1 min, Python package only
sudo ./build/update-gesture-engine.sh --deps     # also reinstalls deps (mediapipe/evdev changed)
sudo ./build/update-gesture-engine.sh --overlay  # also resyncs build/overlay/
                                                  # (phoc.ini, udev, dconf, systemd, autostart)
```

Without `--overlay`, changes to `build/overlay/` (e.g. `phoc.ini`) require a
full rebuild (`build-image.sh`).

## Run

```sh
./run.sh                # fullscreen 1920x1080, with webcam (needs sudo for /dev/bus/usb)
./run.sh --no-cam       # without webcam
./run.sh --windowed     # resizable GTK window instead of fullscreen (debug)
./run.sh --vnc          # no local display, exposes VNC on :1 — isolated inspection
                         # without touching the host desktop
./run.sh --headless     # no display at all, serial console only
```

The VM boots, autologs in as the `musashi` user on tty1, and brings up Phosh
(Android-style home screen with an app grid) and the gesture engine. SSH is
available at `ssh -p 2222 musashi@localhost` (password `musashi`).

Different webcam? `WEBCAM_VID=0x1234 WEBCAM_PID=0xabcd ./run.sh` (see
`lsusb`).

## Gestures

See [docs/GESTURES.md](docs/GESTURES.md). Fine-tuning lives in
`/etc/musashi/config.toml` inside the guest (pinch thresholds, smoothing,
active camera region).

## Structure

- `build/` — image build scripts + rootfs overlay
- `gesture-engine/` — the `musashi_gestures` Python package (MediaPipe → uinput)
- `run.sh` — boots the VM
- `out/` — generated artifacts (image, kernel, initrd); **not versioned**
  (`.gitignore`) — regenerate with `build/build-image.sh`
- `logs/` — local boot/build logs; **not versioned**
- `docs/` — technical decision history and gesture semantics

## About the pivot

This repo started as an OS controlled entirely by gestures ("HaikuOS"). On
2026-08-19 the scope was redefined into a voice-driven personal multi-LLM
mainframe (see `ROADMAP.md`), with the acoustic volumetric interface demoted
to a parallel R&D track and the gesture-engine repurposed as the driver for
one surface among several. The `haiku*` → `musashi*` rename in this repo
(guest user, Python package, paths, image) already reflects the new name;
the architectural restructuring (core + surfaces, see Plano Diretor) hasn't
started yet — what exists today is the base of the old prototype, functional
and validated (M1–M6), ready to become the base of the
`musashi-core`/`musashi-surface` image in sprint S0.
