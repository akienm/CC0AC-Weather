#!/usr/bin/env bash
# Build the ready-to-flash SD card image: Raspberry Pi OS Lite (64-bit) with
# CC0AC-Weather installed by setup/install-pi.sh --image.
#
#   sudo setup/build-image.sh [OUT_DIR]        # default OUT_DIR: ./image-build
#
# Run it on a 64-bit Raspberry Pi (or any arm64 Debian machine): the packages are
# installed by running the image's own apt inside it, which needs the same CPU.
# Needs about 8 GB free, and takes 20-40 minutes on a Pi 4 (most of it compressing).
#
# Out comes cc0ac-weather-<date>.img.xz, its .sha256, and os_list.json, which lets
# Raspberry Pi Imager list it and offer its settings screen (Wi-Fi, user, time zone).
# Users flash it with Raspberry Pi Imager and set Wi-Fi there.
set -euo pipefail

PIOS_URL=${PIOS_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}
GROW_MB=1024
HERE=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(mkdir -p "${1:-image-build}" && cd "${1:-image-build}" && pwd)
DATE=$(date +%Y-%m-%d)
NAME=cc0ac-weather-$DATE
RELEASE_URL=${RELEASE_URL:-https://github.com/akienm/CC0AC-Weather/releases/latest/download}

[ "$(id -u)" = 0 ] || { echo "Run as root: sudo $0 $*" >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || { echo "Run this on a 64-bit Pi (arm64); this machine is $(uname -m)." >&2; exit 1; }
for tool in curl xz losetup parted resize2fs e2fsck sha256sum git; do
    command -v $tool >/dev/null || { echo "Missing $tool: sudo apt install curl xz-utils parted e2fsprogs git" >&2; exit 1; }
done
if [ -n "$(git -C "$HERE" status --porcelain --untracked-files=no)" ]; then
    echo "Note: $HERE has uncommitted changes; the image gets the last commit only." >&2
fi

say() { printf '\n== %s\n' "$*"; }
MNT=$OUT/mnt LOOP=""
cleanup() {
    set +e
    for m in boot/firmware dev/pts dev proc sys run ""; do
        mountpoint -q "$MNT/$m" && umount -l "$MNT/$m"
    done
    [ -n "$LOOP" ] && losetup -d "$LOOP"
}
trap cleanup EXIT

say "Raspberry Pi OS Lite (64-bit)"
cd "$OUT"
BASE_URL=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$PIOS_URL")
BASE=$(basename "$BASE_URL")
echo "$BASE_URL"
if [ ! -e "$BASE" ]; then
    curl -fL --progress-bar -o "$BASE.part" "$BASE_URL"
    mv "$BASE.part" "$BASE"
fi
curl -fsSL -o "$BASE.sha256" "$BASE_URL.sha256"
sha256sum -c "$BASE.sha256"

say "Unpacking and making room"
xz -dc "$BASE" > "$NAME.img"
truncate -s +${GROW_MB}M "$NAME.img"
parted -s "$NAME.img" resizepart 2 100%
LOOP=$(losetup -Pf --show "$NAME.img")
e2fsck -pf "${LOOP}p2" || [ $? -le 1 ]
resize2fs "${LOOP}p2" >/dev/null

say "Installing into the image"
mkdir -p "$MNT"
mount "${LOOP}p2" "$MNT"
mount "${LOOP}p1" "$MNT/boot/firmware"
for m in dev dev/pts proc sys run; do mount --bind "/$m" "$MNT/$m"; done
# apt inside the image needs this machine's name servers while it runs.
mv "$MNT/etc/resolv.conf" "$MNT/etc/resolv.conf.cc0ac-saved" 2>/dev/null || true
cp -L /etc/resolv.conf "$MNT/etc/resolv.conf"
git clone -q "$HERE" "$MNT/tmp/cc0ac-src"
chroot "$MNT" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
    bash /tmp/cc0ac-src/setup/install-pi.sh --image --source /tmp/cc0ac-src
rm -rf "$MNT/tmp/cc0ac-src"
chroot "$MNT" apt-get clean
rm -f "$MNT/etc/resolv.conf"
mv "$MNT/etc/resolv.conf.cc0ac-saved" "$MNT/etc/resolv.conf" 2>/dev/null || true
echo "CC0AC-Weather $(git -C "$HERE" rev-parse --short HEAD) on $BASE, built $DATE" > "$MNT/etc/cc0ac-weather-image"
cleanup; LOOP=""; trap - EXIT

say "Compressing (the slow part)"
SIZE=$(stat -c %s "$NAME.img")
IMG_SHA=$(sha256sum "$NAME.img" | cut -d' ' -f1)
xz -T0 -6 -f "$NAME.img"
sha256sum "$NAME.img.xz" > "$NAME.img.xz.sha256"
XZ_SIZE=$(stat -c %s "$NAME.img.xz")

# Raspberry Pi Imager reads this to list the image and to know how to apply its
# settings screen: Pi OS from trixie on takes them through cloud-init.
INIT=systemd
case "$BASE" in *trixie*) INIT=cloudinit-rpi ;; esac
cat > os_list.json <<EOF
{
  "os_list": [
    {
      "name": "CC0AC-Weather",
      "description": "Weather station: plug your AcuRite Access hub into the Pi. Raspberry Pi OS Lite (64-bit) $DATE.",
      "url": "$RELEASE_URL/$NAME.img.xz",
      "release_date": "$DATE",
      "extract_size": $SIZE,
      "extract_sha256": "$IMG_SHA",
      "image_download_size": $XZ_SIZE,
      "devices": ["pi5-64bit", "pi4-64bit", "pi3-64bit"],
      "init_format": "$INIT"
    }
  ]
}
EOF
say "Built"
ls -l "$OUT/$NAME.img.xz" "$OUT/$NAME.img.xz.sha256" "$OUT/os_list.json"
