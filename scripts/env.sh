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
