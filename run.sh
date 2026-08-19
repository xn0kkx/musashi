#!/usr/bin/env bash
# Boot MusashiOS in QEMU/KVM with USB webcam passthrough.
#
# Usage:
#   ./run.sh              # boot full-screen (needs access to /dev/bus/usb)
#   ./run.sh --no-cam     # boot without webcam
#   ./run.sh --headless   # no graphical window (serial console only)
#   ./run.sh --windowed   # windowed GTK display instead of full-screen (debug)
#   ./run.sh --vnc[=N]    # no local display, expose VNC on :N (default 1) —
#                          # for isolated inspection without touching the host
#                          # desktop; connect with any VNC client to :N
#
# Webcam USB IDs can be overridden: WEBCAM_VID=0x1234 WEBCAM_PID=0xabcd ./run.sh
set -euo pipefail

cd "$(dirname "$0")"

OUT=out
IMG="$OUT/musashi.qcow2"
KERNEL="$OUT/vmlinuz"
INITRD="$OUT/initrd.img"

WEBCAM_VID="${WEBCAM_VID:-0x6211}"   # XIFT Web Camera
WEBCAM_PID="${WEBCAM_PID:-0xe904}"

CAM=1
DISPLAY_ARGS=(-display gtk,gl=off,full-screen=on,window-close=off)
VIDEO_PARAM="video=Virtual-1:1920x1080"
for arg in "$@"; do
    case "$arg" in
        --no-cam)   CAM=0 ;;
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
    -serial mon:stdio
)

if [[ "$CAM" == 1 ]]; then
    ARGS+=(-device "usb-host,bus=xhci.0,vendorid=$WEBCAM_VID,productid=$WEBCAM_PID")
    # USB passthrough needs rw access to the device node under /dev/bus/usb
    if ! lsusb -d "${WEBCAM_VID#0x}:${WEBCAM_PID#0x}" >/dev/null 2>&1; then
        echo "warning: webcam ${WEBCAM_VID}:${WEBCAM_PID} not found on host (lsusb)" >&2
    fi
fi

exec qemu-system-x86_64 "${ARGS[@]}"
