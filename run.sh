#!/usr/bin/env bash
# Boot MusashiOS in QEMU/KVM with USB webcam passthrough.
#
# Usage:
#   ./run.sh              # boot full-screen (needs access to /dev/bus/usb)
#   ./run.sh --no-cam     # boot without webcam
#   ./run.sh --no-audio   # boot without the mic/speaker device
#   ./run.sh --headless   # no graphical window (serial console only)
#   ./run.sh --windowed   # windowed GTK display instead of full-screen (debug)
#   ./run.sh --vnc[=N]    # no local display, expose VNC on :N (default 1) —
#                          # for isolated inspection without touching the host
#                          # desktop; connect with any VNC client to :N
#
# Webcam USB IDs can be overridden: WEBCAM_VID=0x1234 WEBCAM_PID=0xabcd ./run.sh
# Audio backend can be overridden: AUDIO_BACKEND=pa ./run.sh   (pipewire|pa|alsa)
set -euo pipefail

cd "$(dirname "$0")"

OUT=out
IMG="$OUT/musashi.qcow2"
KERNEL="$OUT/vmlinuz"
INITRD="$OUT/initrd.img"

WEBCAM_VID="${WEBCAM_VID:-0x6211}"   # XIFT Web Camera
WEBCAM_PID="${WEBCAM_PID:-0xe904}"

VSOCK_CID="${VSOCK_CID:-3}"          # guest context id for musashi-effector

AUDIO_BACKEND="${AUDIO_BACKEND:-pipewire}"   # host-side sound server QEMU talks to

CAM=1
AUDIO=1
DISPLAY_ARGS=(-display gtk,gl=off,full-screen=on,window-close=off)
VIDEO_PARAM="video=Virtual-1:1920x1080"
for arg in "$@"; do
    case "$arg" in
        --no-cam)   CAM=0 ;;
        --no-audio) AUDIO=0 ;;
        --headless) DISPLAY_ARGS=(-display none); VIDEO_PARAM="" ;;
        --windowed) DISPLAY_ARGS=(-display gtk,gl=off) ;;
        --vnc)      DISPLAY_ARGS=(-display none -vnc :1) ;;
        --vnc=*)    DISPLAY_ARGS=(-display none -vnc ":${arg#--vnc=}") ;;
        *) echo "unknown option: $arg" >&2; exit 1 ;;
    esac
done

for f in "$IMG" "$KERNEL" "$INITRD"; do
    [[ -e "$f" ]] || { echo "missing $f — run: sudo ./build/build-image.sh" >&2; exit 1; }
done

APPEND="root=/dev/vda rw console=tty1 console=ttyS0 quiet"
[[ -n "$VIDEO_PARAM" ]] && APPEND="$APPEND $VIDEO_PARAM"

ARGS=(
    -enable-kvm -cpu host -smp 4 -m 4096
    -kernel "$KERNEL" -initrd "$INITRD"
    -append "$APPEND"
    -drive "file=$IMG,if=virtio"
    -device virtio-vga,xres=1920,yres=1080
    "${DISPLAY_ARGS[@]}"
    -device qemu-xhci,id=xhci
    -netdev user,id=n0,hostfwd=tcp::2222-:22
    -device virtio-net-pci,netdev=n0
    # Host <-> guest intent transport for musashi-effector. CIDs 0-2 are
    # reserved (hypervisor/local/host), so the guest gets 3. Reach it from the
    # host with e.g. socat - VSOCK-CONNECT:3:5000 — no network, no open port.
    -device "vhost-vsock-pci,guest-cid=$VSOCK_CID"
    -serial mon:stdio
)

if [[ "$AUDIO" == 1 ]]; then
    # The voice loop runs INSIDE the guest, so the VM needs a real duplex sound
    # card: hda-duplex is line-out + line-in, i.e. the guest sees one ALSA card
    # it can both play through and capture from. The host end is PipeWire,
    # which is what this machine actually runs; `-audiodev help` lists the
    # alternatives if that ever changes.
    #
    # hda-micro (speaker + microphone) is the same codec with different jack
    # labelling; hda-duplex is used because line-in is not subject to the
    # capture-source auto-muting some guests apply to a "microphone" jack.
    if ! qemu-system-x86_64 -audiodev help 2>/dev/null | grep -qx "$AUDIO_BACKEND"; then
        echo "warning: QEMU has no '$AUDIO_BACKEND' audio backend; try AUDIO_BACKEND=pa or --no-audio" >&2
    fi
    ARGS+=(
        -audiodev "$AUDIO_BACKEND,id=snd0"
        -device intel-hda
        -device hda-duplex,audiodev=snd0
    )
fi

if [[ "$CAM" == 1 ]]; then
    ARGS+=(-device "usb-host,bus=xhci.0,vendorid=$WEBCAM_VID,productid=$WEBCAM_PID")
    # USB passthrough needs rw access to the device node under /dev/bus/usb
    if ! lsusb -d "${WEBCAM_VID#0x}:${WEBCAM_PID#0x}" >/dev/null 2>&1; then
        echo "warning: webcam ${WEBCAM_VID}:${WEBCAM_PID} not found on host (lsusb)" >&2
    fi
fi

exec qemu-system-x86_64 "${ARGS[@]}"
