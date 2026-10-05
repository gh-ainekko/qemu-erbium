#!/usr/bin/env bash
# Fetch the external source trees at pinned revisions and apply the erbium patch series.
#   ext/qemu        Xilinx QEMU        @ $QEMU_SHA     + qemu-patches/
#   et-platform     aifoundry et-platform @ $ETP_SHA   + sysemu-patches/
#   ext/linux       linux-$LINUX_VER tarball          + linux/patches/
#   linux/rootfs/busybox  static arm64 busybox from Ubuntu
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
"$R/scripts/preflight.sh" fetch
QEMU_URL=${QEMU_URL:-https://github.com/Xilinx/qemu.git}
QEMU_SHA=${QEMU_SHA:-59fb95c62a25821b5bd8fc594af5b7a6bdaef4dd}
ETP_URL=${ETP_URL:-https://github.com/aifoundry-org/et-platform.git}
ETP_SHA=${ETP_SHA:-836a4ab600e93c3059bb58c898edbc37744cd8d0}
LINUX_VER=${LINUX_VER:-6.12.48}
BUSYBOX_DEB=${BUSYBOX_DEB:-http://ports.ubuntu.com/pool/main/b/busybox/busybox-static_1.36.1-6ubuntu3.1_arm64.deb}

git_at() { # dir url sha branch patchdir
  local dir=$1 url=$2 sha=$3 branch=$4 patches=$5
  if [ -d "$dir/.git" ]; then echo "$dir: present"; return; fi
  mkdir -p "$dir"; git -C "$dir" init -q; git -C "$dir" remote add origin "$url"
  git -C "$dir" fetch -q --depth 1 origin "$sha"
  git -C "$dir" checkout -q -b "$branch" FETCH_HEAD
  git -C "$dir" -c user.name=erbium-bootstrap -c user.email=bootstrap@erbium.local am -q "$patches"/*.patch
  echo "$dir: $(git -C "$dir" log --oneline | wc -l) commits on $branch"
}

git_at "$R/ext/qemu" "$QEMU_URL" "$QEMU_SHA" erbium "$R/qemu-patches"
git_at "$R/et-platform" "$ETP_URL" "$ETP_SHA" erbium-qemu-backend "$R/sysemu-patches"

if [ ! -d "$R/ext/linux" ]; then
  mkdir -p "$R/ext"
  curl -fsSL "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$LINUX_VER.tar.xz" -o "$R/ext/linux.tar.xz"
  tar xf "$R/ext/linux.tar.xz" -C "$R/ext" && mv "$R/ext/linux-$LINUX_VER" "$R/ext/linux" && rm "$R/ext/linux.tar.xz"
  for p in "$R"/linux/patches/*.patch; do patch -s -p1 -d "$R/ext/linux" < "$p"; done
  echo "ext/linux: $LINUX_VER + $(ls "$R"/linux/patches | wc -l) patches"
else
  echo "ext/linux: present"
fi

if [ ! -f "$R/linux/rootfs/busybox" ]; then
  tmp=$(mktemp -d); curl -fsSL "$BUSYBOX_DEB" -o "$tmp/bb.deb"; dpkg-deb -x "$tmp/bb.deb" "$tmp/x"
  cp "$tmp/x/usr/bin/busybox" "$R/linux/rootfs/busybox"; rm -rf "$tmp"
  echo "busybox: fetched"
fi
