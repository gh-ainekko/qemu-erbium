#!/usr/bin/env bash
# End-to-end smoke: erbium_emu (mailbox worker) + QEMU erbium-xspi qtests.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
SOCK=${SOCK:-/tmp/erb.sock}
MRAM=${MRAM:-/tmp/mram.img}
QEMU_BUILD=$R/ext/qemu/build
EMU=$R/build/sw-sysemu/erbium_emu
FW=$R/et-platform/sw-sysemu/tests/erbium/build/mailbox_worker.elf

[ -x "$EMU" ] || { echo "build erbium_emu first (docs/sysemu-notes.md)"; exit 1; }
[ -f "$FW" ] || { echo "build mailbox_worker.elf first (docs/sysemu-notes.md)"; exit 1; }
[ -f "$MRAM" ] || truncate -s 16M "$MRAM"
rm -f "$SOCK"

"$EMU" -minions 0x1 -single_thread -elf "$FW" --api-socket "$SOCK" --mram-file "$MRAM" \
    > /tmp/erbium_emu.log 2>&1 &
EMU_PID=$!
trap 'kill $EMU_PID 2>/dev/null || true' EXIT
for i in $(seq 1 50); do [ -S "$SOCK" ] && break; sleep 0.1; done

cd "$QEMU_BUILD"
ERBIUM_BACKEND_SOCKET=$SOCK ERBIUM_MRAM_FILE=$MRAM QTEST_QEMU_BINARY=./qemu-system-aarch64 \
    tests/qtest/erbium-xspi-test "$@"
