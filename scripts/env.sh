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

# One explicitly selected host export. Validate before either emulator starts.
# QEMU keyvals use commas as separators; never let a pathname inject options.
prepare_share() {
  SHARE_ARGS=(); SHARE_CMDLINE=""
  [ -n "${SHARE_MODE:-}" ] || return 0
  case "$SHARE_MODE" in ro|rw) ;; *) echo 'Invalid share mode' >&2; return 2;; esac
  case "$SHARE_DIR" in *','*|*$'\n'*|*$'\r'*) echo 'Share paths must not contain commas or newlines' >&2; return 2;; esac
  [ -d "$SHARE_DIR" ] || { echo "Share directory does not exist: $SHARE_DIR" >&2; return 2; }
  # Keep filename trailing newlines until validation; command substitution
  # otherwise strips them and could silently select a different sibling tree.
  SHARE_DIR=$(CDPATH= cd -- "$SHARE_DIR" && pwd -P && printf '.') || return 2
  SHARE_DIR=${SHARE_DIR%.}
  SHARE_DIR=${SHARE_DIR%$'\n'}
  case "$SHARE_DIR" in *','*|*$'\n'*|*$'\r'*) echo 'Resolved share path must not contain commas or newlines' >&2; return 2;; esac
  local readonly=on
  [ "$SHARE_MODE" != rw ] || readonly=off
  # This pinned kernel/QEMU pair showed a queued-spinlock oops/hang with
  # concurrent 9P reads under MTTCG. Serialize vCPU execution for shared-folder
  # sessions; this retains both guest CPUs and does not affect the UART peer.
  SHARE_ARGS=(-accel tcg,thread=single -fsdev "local,id=hostshare,path=$SHARE_DIR,security_model=mapped-xattr,readonly=$readonly,multidevs=remap"
              -device virtio-9p-device,fsdev=hostshare,mount_tag=hostshare)
  SHARE_CMDLINE="erbium.share=$SHARE_MODE"
}
