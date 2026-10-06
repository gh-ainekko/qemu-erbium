# Sourced by the run scripts: locate binaries in a dist tree (release tarball, or ./dist in a
# source checkout) or fall back to the build trees.
R=${R:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
pick() { for f in "$@"; do [ -e "$f" ] && { echo "$f"; return; }; done; echo "${!#}"; }
QEMU=${QEMU:-$(pick "$R/bin/qemu-system-aarch64" "$R/dist/bin/qemu-system-aarch64" "$R/ext/qemu/build/qemu-system-aarch64")}
QTEST=${QTEST:-$(pick "$R/tests/erbium-xspi-test" "$R/dist/tests/erbium-xspi-test" "$R/ext/qemu/build/tests/qtest/erbium-xspi-test")}
EMU=${EMU:-$(pick "$R/bin/erbium_emu" "$R/dist/bin/erbium_emu" "$R/build/sw-sysemu/erbium_emu")}
FW=${FW:-$(pick "$R/firmware/mailbox_worker.elf" "$R/dist/firmware/mailbox_worker.elf" "$R/et-platform/sw-sysemu/tests/erbium/build/mailbox_worker.elf")}
IMAGE=${IMAGE:-$(pick "$R/linux/Image" "$R/dist/linux/Image" "$R/build/linux/arch/arm64/boot/Image")}
# an installed QEMU needs its data dir
QEMU_ARGS=()
for d in "$R/share/qemu" "$R/dist/share/qemu"; do
  if [ -d "$d" ] && [ "$QEMU" = "$(dirname "$d")/../bin/qemu-system-aarch64" -o "$QEMU" = "${d%/share/qemu}/bin/qemu-system-aarch64" ]; then
    QEMU_ARGS=(-L "$d"); break
  fi
done

# Check runtime artifacts without requiring source or compiler dependencies.
# Keep these functions here so source checkouts and release tarballs agree.
runtime_hint() {
  if [ -f "$R/bootstrap.sh" ]; then
    echo "From $R: run J=2 ./bootstrap.sh to build the complete stack." >&2
    echo "For just the stub guest, build both targets: scripts/build-all.sh qemu && scripts/build-all.sh linux" >&2
  else
    echo "Re-extract the complete binary distribution, or check QEMU/IMAGE overrides." >&2
  fi
}
require_qemu() {
  if ! command -v "$QEMU" >/dev/null 2>&1; then
    echo "Missing or non-executable Erbium QEMU: $QEMU" >&2
    echo "A Linux-only build produces the guest Image, not the emulator. Stock Ubuntu QEMU does not include this device." >&2
    runtime_hint
    return 1
  fi
}
require_guest() {
  require_qemu || return 1
  if [ ! -s "$IMAGE" ]; then
    echo "Missing or empty Linux guest Image: $IMAGE" >&2
    runtime_hint
    return 1
  fi
}
