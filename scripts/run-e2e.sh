#!/usr/bin/env bash
# End-to-end smoke: erbium_emu (mailbox worker) + QEMU erbium-xspi qtests + Linux guest test.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd); . "$R/scripts/env.sh"
SOCK=${SOCK:-/tmp/erb.sock}
MRAM=${MRAM:-/tmp/mram.img}

[ -x "$EMU" ] || { echo "erbium_emu not found ($EMU); run bootstrap.sh"; exit 1; }
[ -f "$FW" ] || { echo "mailbox_worker.elf not found ($FW); run bootstrap.sh"; exit 1; }
[ -x "$QTEST" ] || { echo "qtest not found ($QTEST); run bootstrap.sh"; exit 1; }
[ -f "$MRAM" ] || truncate -s 16M "$MRAM"
rm -f "$SOCK"

"$EMU" -minions 0x1 -single_thread -elf "$FW" --api-socket "$SOCK" --mram-file "$MRAM" \
    > /tmp/erbium_emu.log 2>&1 &
EMU_PID=$!
trap 'kill $EMU_PID 2>/dev/null || true' EXIT
for i in $(seq 1 50); do [ -S "$SOCK" ] && break; sleep 0.1; done

echo "== qtests (stub backend)"
QTEST_QEMU_BINARY="$QEMU" "$QTEST"
echo "== qtests (erbium_emu backend)"
ERBIUM_BACKEND_SOCKET=$SOCK ERBIUM_MRAM_FILE=$MRAM QTEST_QEMU_BINARY="$QEMU" "$QTEST"

# Linux guest against the same backend (skip with NO_LINUX=1)
if [ -z "${NO_LINUX:-}" ] && [ -f "$IMAGE" ]; then
  echo "== Linux guest (erbium_emu backend)"
  out=$("$R/scripts/run-linux.sh" --test --backend "$SOCK" --mram "$MRAM" 2>&1)
  echo "$out" | grep -E 'erbium-xspi spi|==|crc32|FAIL|ALL TESTS|RESULT'
  echo "$out" | grep -q 'ERBIUM-TEST-RESULT 0'
fi
