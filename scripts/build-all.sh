#!/usr/bin/env bash
# Build everything into build/ and assemble a relocatable dist/ tree.
#   dist/bin/qemu-system-aarch64  dist/share/qemu/   dist/tests/erbium-xspi-test
#   dist/bin/erbium_emu           dist/firmware/mailbox_worker.elf
#   dist/linux/Image (initramfs incl. busybox + erbctl built in)
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
J=${J:-$(nproc)}
ONLY=${1:-all}
"$R/scripts/preflight.sh" build "$ONLY"
export CCACHE_DIR=${CCACHE_DIR:-$R/build/ccache}
command -v ccache >/dev/null && export CC="ccache gcc" CXX="ccache g++" || true

build_qemu() {
  mkdir -p "$R/ext/qemu/build"
  if [ ! -f "$R/ext/qemu/build/config.status" ]; then
    (cd "$R/ext/qemu/build" && ../configure --target-list=aarch64-softmmu --prefix="$R/dist" \
      --enable-fdt --disable-docs --disable-werror --enable-slirp --enable-gcrypt \
      --disable-gtk --disable-sdl --disable-vnc --disable-spice --disable-opengl \
      --disable-virglrenderer --disable-gnutls --disable-nettle --disable-libssh --disable-curl \
      --disable-libnfs --disable-libiscsi --disable-rbd --disable-glusterfs --disable-smartcard \
      --disable-usb-redir --disable-libusb --disable-xen --disable-bpf --disable-guest-agent \
      --disable-tools --disable-plugins --disable-lzo --disable-snappy --disable-bzip2 \
      --disable-lzfse --disable-zstd --disable-brlapi --disable-curses --disable-alsa \
      --disable-pa --disable-pipewire --disable-oss --disable-jack --disable-sndio \
      --disable-vhost-user --disable-vhost-net --disable-vhost-kernel --disable-vhost-vdpa \
      --disable-libvduse --disable-vduse-blk-export --disable-user --disable-linux-user \
      --disable-bsd-user --disable-tpm --disable-seccomp --disable-cap-ng --disable-attr \
      --disable-linux-aio --disable-linux-io-uring --disable-numa --disable-rdma --disable-vde \
      --disable-netmap --disable-libudev --disable-mpath --disable-l2tpv3 --disable-af-xdp \
      --disable-debug-info ${QEMU_CONFIGURE_EXTRA:-} > "$R/build/qemu-configure.log" 2>&1) \
      || { tail -30 "$R/build/qemu-configure.log"; exit 1; }
  fi
  ninja -C "$R/ext/qemu/build" -j"$J" qemu-system-aarch64 tests/qtest/erbium-xspi-test
  ninja -C "$R/ext/qemu/build" install >/dev/null
  # the Versal machine needs no firmware blobs; drop ~300 MB of edk2/seabios images
  find "$R/dist/share/qemu" -maxdepth 1 -type f \( -name '*.fd' -o -name '*.bin' -o -name '*.rom' -o -name '*.img' -o -name '*.dtb' -o -name '*.bz2' \) -delete
  rm -rf "$R/dist/share/applications" "$R/dist/share/icons" "$R/dist/include" "$R/dist/libexec" "$R/dist/var" 2>/dev/null || true
  mkdir -p "$R/dist/tests"; cp "$R/ext/qemu/build/tests/qtest/erbium-xspi-test" "$R/dist/tests/"
}

build_sysemu() {
  cmake -S "$R/et-platform/erbium-hal" -B "$R/build/erbium-hal" -DCMAKE_INSTALL_PREFIX="$R/build/prefix" >/dev/null
  cmake --install "$R/build/erbium-hal" >/dev/null
  cmake -S "$R/et-platform/sw-sysemu" -B "$R/build/sw-sysemu" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS_RELEASE=-O2 -DCMAKE_C_FLAGS_RELEASE=-O2 \
        -DCMAKE_PREFIX_PATH="$R/build/prefix" >/dev/null
  ninja -C "$R/build/sw-sysemu" -j"$J" erbium_emu
  mkdir -p "$R/dist/bin"; cp "$R/build/sw-sysemu/erbium_emu" "$R/dist/bin/"
}

build_firmware() {
  local T="$R/et-platform/sw-sysemu/tests/erbium"
  mkdir -p "$T/build"
  sed 's/mova\.m\.x zero/.word 0xD600107B  \/* mova.m.x zero *\//' "$T/common/boot.S" > "$T/build/boot.S"
  riscv64-unknown-elf-gcc -I"$T/include" -I"$T/common" \
    -Wa,--defsym,validation0=0x8d0 -Wa,--defsym,tensor_mask=0x805 \
    -Wall -Wextra -Werror -nostdlib -O2 -g -mcmodel=medany -march=rv64imfc -mabi=lp64f \
    -T"$T/common/erbium.ld" -Wl,--section-start=bootrom=0x200a000 -Wl,--no-warn-rwx-segments \
    -o "$T/build/mailbox_worker.elf" "$T/host/mailbox_worker.c" "$T/build/boot.S" "$T/common/crt.S" "$T/common/trap.S"
  mkdir -p "$R/dist/firmware"; cp "$T/build/mailbox_worker.elf" "$R/dist/firmware/"
}

build_linux() {
  aarch64-linux-gnu-gcc -O2 -static -Wall -o "$R/build/erbctl" "$R/linux/tools/erbctl.c"
  "$R/scripts/configure-linux.sh"
  make -C "$R/ext/linux" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
    O="$R/build/linux" -j"$J" Image 2>&1 | tee "$R/build/linux-build.log"
  [ -f "$R/build/linux/arch/arm64/boot/Image" ]
  mkdir -p "$R/dist/linux"; cp "$R/build/linux/arch/arm64/boot/Image" "$R/dist/linux/"
}

mkdir -p "$R/build" "$R/dist"
case "$ONLY" in
  all) build_qemu; build_sysemu; build_firmware; build_linux ;;
  qemu) build_qemu ;; sysemu) build_sysemu ;; firmware) build_firmware ;; linux) build_linux ;;
  *) echo "usage: $0 [all|qemu|sysemu|firmware|linux]"; exit 2 ;;
esac
cp "$R/build/erbctl" "$R/dist/bin/erbctl-aarch64" 2>/dev/null || true
echo "dist/ ready:"; find "$R/dist" -maxdepth 2 -type f | grep -v share/qemu | sed "s|$R/||"
