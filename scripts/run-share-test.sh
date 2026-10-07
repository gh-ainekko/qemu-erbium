#!/usr/bin/env bash
# Real guest test, usable unchanged in a binary-only distribution.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d /tmp/erbium-share-test.XXXXXX)
PID=
cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [ -n "$PID" ]; then
    kill -TERM -- "-$PID" 2>/dev/null || true
    kill -TERM "$PID" 2>/dev/null || true
    sleep 0.1
    kill -KILL -- "-$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  if [ "$status" = 0 ]; then rm -rf "$T"; else echo "Share test logs: $T" >&2; fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for mode in ro rw; do
  dir="$T/shared folder $mode"
  mkdir "$dir"
  printf 'hello from host\n' > "$dir/input.txt"
  printf 'original on host\n' > "$dir/live.txt"
  option=--share; [ "$mode" != rw ] || option=--share-rw
  timeout -k 2 90 "$R/scripts/run-linux.sh" "$option" "$dir" --mram "$T/mram-$mode" \
    -append "console=ttyAMA0 erbium.share=$mode erbium.sharetest" \
    < /dev/null > "$T/$mode.log" 2>&1 &
  PID=$!
  for _ in $(seq 1 600); do
    grep -q '^ERBIUM-SHARE-READY' "$T/$mode.log" && break
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.1
  done
  printf 'updated on host\n' > "$dir/live.new"
  mv "$dir/live.new" "$dir/live.txt"
  if ! wait "$PID"; then cat "$T/$mode.log" >&2; exit 1; fi
  PID=
  cat "$T/$mode.log"
  tr -d '\r' < "$T/$mode.log" | grep -qx 'ERBIUM-SHARE-TEST-RESULT 0'
  if [ "$mode" = ro ]; then
    [ ! -e "$dir/output.txt" ]
    [ "$(cat "$dir/input.txt")" = 'hello from host' ]
  else
    [ "$(cat "$dir/output.txt")" = 'hello from guest' ]
    [ "$(cat "$dir/input.txt")" = $'hello from host\n appended by guest' ]
  fi
done
echo 'Host shared-folder read-only/read-write and live-update tests PASSED'
