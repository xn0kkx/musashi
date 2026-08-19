#!/usr/bin/env bash
# Fast path for iterating on gesture-engine/ Python code: patches the
# existing qcow2 in place via NBD instead of a full debootstrap rebuild.
#   ~1 min vs ~45 min for ./build/build-image.sh
#
#   sudo ./build/update-gesture-engine.sh            # reinstall musashi-gestures only
#   sudo ./build/update-gesture-engine.sh --deps      # also reinstall its dependencies
#                                                      # (mediapipe/evdev changed)
#   sudo ./build/update-gesture-engine.sh --overlay   # also resync build/overlay/
#                                                      # (phoc.ini, udev rules, dconf,
#                                                      # systemd units, autostart, ...)
#
# Does NOT touch packages or users — for those, use ./build/build-image.sh.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "must run as root (nbd mount)" >&2; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMG="$REPO/out/musashi.qcow2"
MNT="$REPO/out/mnt-nbd"
NBD=/dev/nbd0
DEPS=0
OVERLAY=0
for arg in "$@"; do
    case "$arg" in
        --deps)    DEPS=1 ;;
        --overlay) OVERLAY=1 ;;
        *) echo "unknown option: $arg" >&2; exit 1 ;;
    esac
done

[[ -e "$IMG" ]] || { echo "missing $IMG — run: sudo ./build/build-image.sh" >&2; exit 1; }

if pgrep -f "qemu-system-x86_64.*musashi" >/dev/null; then
    echo "a MusashiOS VM is running — stop it first (sudo pkill -f qemu-system-x86_64)" >&2
    exit 1
fi

modprobe nbd max_part=8
mkdir -p "$MNT"

cleanup() {
    for sub in dev/pts dev proc sys; do
        mountpoint -q "$MNT/$sub" 2>/dev/null && umount "$MNT/$sub"
    done
    mountpoint -q "$MNT" && umount "$MNT"
    qemu-nbd --disconnect "$NBD" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> mounting $IMG via nbd"
qemu-nbd --connect="$NBD" "$IMG"
sleep 1
mount "$NBD" "$MNT"

echo "==> syncing gesture-engine sources"
rm -rf "$MNT/opt/musashi/gesture-engine"
cp -a "$REPO/gesture-engine" "$MNT/opt/musashi/"
cp "$REPO/gesture-engine/musashi_gestures/config.toml" "$MNT/etc/musashi/config.toml"

if [[ "$OVERLAY" == 1 ]]; then
    echo "==> syncing build/overlay/"
    cp -a "$REPO/build/overlay/." "$MNT/"
fi

echo "==> reinstalling into venv (chroot)"
cp /etc/resolv.conf "$MNT/etc/resolv.conf"
mount -t proc proc "$MNT/proc"
mount -t sysfs sys "$MNT/sys"
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts"

if [[ "$DEPS" == 1 ]]; then
    chroot "$MNT" /opt/gesture-engine/venv/bin/pip install --no-cache-dir --force-reinstall /opt/musashi/gesture-engine
else
    chroot "$MNT" /opt/gesture-engine/venv/bin/pip install --no-cache-dir --force-reinstall --no-deps /opt/musashi/gesture-engine
fi

if [[ "$OVERLAY" == 1 ]]; then
    chroot "$MNT" dconf update
fi

echo "==> done — boot with ./run.sh to pick up changes"
