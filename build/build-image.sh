#!/usr/bin/env bash
# Build the MusashiOS disk image from scratch (reproducible, no manual steps).
#
#   sudo ./build/build-image.sh
#
# Produces:
#   out/musashi.qcow2   root filesystem (partitionless ext4, root=/dev/vda)
#   out/vmlinuz         guest kernel  (QEMU direct boot, no bootloader)
#   out/initrd.img      guest initramfs
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "must run as root (debootstrap/chroot/mount)" >&2; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/out"
MNT="$OUT/mnt"
RAW="$OUT/musashi.raw"
IMG_SIZE=6G
SUITE=trixie
MIRROR=http://deb.debian.org/debian

cleanup() {
    for sub in dev/pts dev proc sys; do
        mountpoint -q "$MNT/$sub" && umount "$MNT/$sub"
    done
    mountpoint -q "$MNT" && umount "$MNT"
}
trap cleanup EXIT

mkdir -p "$OUT" "$MNT"

echo "==> creating $IMG_SIZE raw image (partitionless ext4)"
rm -f "$RAW"
truncate -s "$IMG_SIZE" "$RAW"
mkfs.ext4 -q -L musashi "$RAW"
mount -o loop "$RAW" "$MNT"

echo "==> debootstrap $SUITE (minbase)"
debootstrap --variant=minbase --arch=amd64 "$SUITE" "$MNT" "$MIRROR"

echo "==> base configuration"
echo musashi > "$MNT/etc/hostname"
cat > "$MNT/etc/hosts" <<'EOF'
127.0.0.1   localhost
127.0.1.1   musashi
EOF
cat > "$MNT/etc/fstab" <<'EOF'
/dev/vda / ext4 defaults 0 1
EOF

echo "==> applying overlay"
cp -a "$REPO/build/overlay/." "$MNT/"

echo "==> copying gesture-engine sources"
mkdir -p "$MNT/opt/musashi"
cp -a "$REPO/gesture-engine" "$MNT/opt/musashi/"
# Editable copy of the config inside the guest (overrides packaged defaults)
mkdir -p "$MNT/etc/musashi"
cp "$REPO/gesture-engine/musashi_gestures/config.toml" "$MNT/etc/musashi/config.toml"

echo "==> chroot setup"
cp /etc/resolv.conf "$MNT/etc/resolv.conf"
mount -t proc proc "$MNT/proc"
mount -t sysfs sys "$MNT/sys"
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts"
install -m 0755 "$REPO/build/chroot-setup.sh" "$MNT/chroot-setup.sh"
chroot "$MNT" /chroot-setup.sh
rm -f "$MNT/chroot-setup.sh"

echo "==> extracting kernel/initrd for QEMU direct boot"
cp "$(ls "$MNT"/boot/vmlinuz-* | sort -V | tail -1)" "$OUT/vmlinuz"
cp "$(ls "$MNT"/boot/initrd.img-* | sort -V | tail -1)" "$OUT/initrd.img"

cleanup
trap - EXIT

echo "==> converting to qcow2"
qemu-img convert -f raw -O qcow2 "$RAW" "$OUT/musashi.qcow2"
rm -f "$RAW"

chown -R "${SUDO_UID:-0}:${SUDO_GID:-0}" "$OUT"
echo "==> done: $OUT/musashi.qcow2 ($(du -h "$OUT/musashi.qcow2" | cut -f1))"
echo "boot it with: ./run.sh"
