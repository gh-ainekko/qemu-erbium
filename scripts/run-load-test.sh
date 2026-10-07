#!/usr/bin/env bash
# Real host-driven load: empty MRAM, held CPU, NO emulator ELF/preload option.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd); . "$R/scripts/env.sh"
require_guest
[ -x "$EMU" ] || { echo "Missing erbium_emu: $EMU" >&2; exit 1; }
T=$(mktemp -d /tmp/erbium-load.XXXXXX)
EMU_PID=
cleanup() {
  if [ -n "$EMU_PID" ]; then kill "$EMU_PID" 2>/dev/null || true; wait "$EMU_PID" 2>/dev/null || true; fi
  rm -rf "$T"
}
trap cleanup EXIT
truncate -s 16M "$T/mram.img"
"$EMU" -single_thread -minions 0x1 --start-held \
  --api-socket "$T/control.sock" --mram-file "$T/mram.img" \
  > "$T/backend.log" 2>&1 &
EMU_PID=$!
for i in $(seq 1 100); do
  [ -S "$T/control.sock" ] && break
  kill -0 "$EMU_PID" 2>/dev/null || break
  sleep 0.05
done
if [ ! -S "$T/control.sock" ]; then cat "$T/backend.log" >&2; exit 1; fi
echo "== Linux host ELF load/start test (empty MRAM, no -elf, held CPU)"
if ! timeout 180 "$R/scripts/run-linux.sh" --load-test \
    --backend "$T/control.sock" --mram "$T/mram.img" > "$T/guest.log" 2>&1; then
  cat "$T/guest.log" "$T/backend.log" >&2
  exit 1
fi
cat "$T/guest.log"
if ! grep -q '^ERBIUM-LOAD-TEST-RESULT 0' "$T/guest.log"; then
  cat "$T/backend.log" >&2
  exit 1
fi
