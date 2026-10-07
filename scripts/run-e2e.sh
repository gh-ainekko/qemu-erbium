#!/usr/bin/env bash
# End-to-end smoke: erbium_emu (mailbox worker) + QEMU erbium-xspi qtests + Linux guest test.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd); . "$R/scripts/env.sh"
SOCK=${SOCK:-/tmp/erb.sock}
MRAM=${MRAM:-/tmp/mram.img}

require_qemu
[ -x "$EMU" ] || { echo "erbium_emu not found ($EMU); run bootstrap.sh"; exit 1; }
[ -f "$FW" ] || { echo "mailbox_worker.elf not found ($FW); run bootstrap.sh"; exit 1; }
[ -x "$QTEST" ] || { echo "qtest not found ($QTEST); run bootstrap.sh"; exit 1; }
if [ -z "${NO_LINUX:-}" ] && [ ! -s "$IMAGE" ]; then
  echo "Linux Image not found ($IMAGE); build it or explicitly set NO_LINUX=1" >&2
  exit 1
fi
[ -f "$MRAM" ] || truncate -s 16M "$MRAM"
rm -f "$SOCK"

"$EMU" -minions 0x1 -single_thread -elf "$FW" --api-socket "$SOCK" --mram-file "$MRAM" \
    > /tmp/erbium_emu.log 2>&1 &
EMU_PID=$!
trap 'kill $EMU_PID 2>/dev/null || true' EXIT
for i in $(seq 1 50); do
  [ -S "$SOCK" ] && break
  kill -0 "$EMU_PID" 2>/dev/null || break
  sleep 0.1
done
if [ ! -S "$SOCK" ]; then
  echo "Backend failed to create $SOCK" >&2
  cat /tmp/erbium_emu.log >&2
  exit 1
fi

echo "== qtests (stub backend)"
QTEST_QEMU_BINARY="$QEMU" "$QTEST"
echo "== qtests (erbium_emu backend)"
ERBIUM_BACKEND_SOCKET=$SOCK ERBIUM_MRAM_FILE=$MRAM QTEST_QEMU_BINARY="$QEMU" "$QTEST"

# Linux guest against the same backend (skip with NO_LINUX=1)
if [ -z "${NO_LINUX:-}" ]; then
  echo "== Linux guest (erbium_emu backend)"
  if ! out=$(timeout 120 "$R/scripts/run-linux.sh" --test --backend "$SOCK" --mram "$MRAM" 2>&1); then
    printf '%s\n' "$out" >&2
    exit 1
  fi
  echo "$out" | grep -E 'erbium-xspi spi|==|crc32|FAIL|ALL TESTS|RESULT'
  echo "$out" | grep -q 'ERBIUM-TEST-RESULT 0'
  "$R/scripts/run-load-test.sh"
fi
