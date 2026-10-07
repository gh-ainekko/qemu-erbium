#!/usr/bin/env bash
# Fresh, held Erbium + Linux host, with a separate physical-peer UART cable.
# No backend ELF preload. Default is an interactive Linux shell.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd); . "$R/scripts/env.sh"
MODE=(); MARKER=
case "${1:-}" in
  '') ;;
  --test) MODE=(--uart-test); MARKER=ERBIUM-UART-TEST-RESULT ;;
  --kotama-test) MODE=(--kotama-test); MARKER=ERBIUM-KOTAMA-UART-TEST-RESULT ;;
  *) echo "usage: $0 [--test | --kotama-test] (KEEP_UART_LOGS=1 retains session files)" >&2; exit 2;;
esac
[ "$#" -le 1 ] || { echo "Too many arguments" >&2; exit 2; }
require_guest
[ -x "$EMU" ] || { echo "Missing erbium_emu: $EMU" >&2; exit 1; }
T=$(mktemp -d /tmp/erbium-uart.XXXXXX)
EMU_PID=
QEMU_PID=
QEMU_GROUP=0
cleanup() {
  result=$?
  trap - EXIT INT TERM
  if [ -n "$QEMU_PID" ]; then
    # GNU timeout owns a separate process group in test mode. Terminate that
    # group as well as its leader, including if the wrapper alone got TERM.
    if [ "$QEMU_GROUP" = 1 ]; then kill -TERM -- "-$QEMU_PID" 2>/dev/null || true; fi
    kill "$QEMU_PID" 2>/dev/null || true
    for _ in $(seq 1 50); do kill -0 "$QEMU_PID" 2>/dev/null || break; sleep 0.02; done
    if [ "$QEMU_GROUP" = 1 ]; then kill -KILL -- "-$QEMU_PID" 2>/dev/null || true; fi
    kill -KILL "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
  fi
  if [ -n "$EMU_PID" ]; then
    kill "$EMU_PID" 2>/dev/null || true
    for _ in $(seq 1 50); do kill -0 "$EMU_PID" 2>/dev/null || break; sleep 0.02; done
    kill -KILL "$EMU_PID" 2>/dev/null || true
    wait "$EMU_PID" 2>/dev/null || true
  fi
  if [ "$result" -ne 0 ] || [ "${KEEP_UART_LOGS:-0}" = 1 ]; then
    echo "UART session files: $T" >&2
  else
    rm -rf "$T"
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
truncate -s 16M "$T/mram.img"
"$EMU" -single_thread -minions 0x1 --start-held \
  --api-socket "$T/control.sock" --mram-file "$T/mram.img" \
  --uart-socket "$T/uart.sock" < /dev/null > "$T/backend.log" 2>&1 &
EMU_PID=$!
for _ in $(seq 1 100); do
  [ -S "$T/control.sock" ] && [ -S "$T/uart.sock" ] && break
  kill -0 "$EMU_PID" 2>/dev/null || break
  sleep 0.05
done
if [ ! -S "$T/control.sock" ] || [ ! -S "$T/uart.sock" ]; then
  cat "$T/backend.log" >&2
  exit 1
fi
ARGS=(--backend "$T/control.sock" --mram "$T/mram.img" --uart-socket "$T/uart.sock")
if [ -z "$MARKER" ]; then
  echo "Fresh held Erbium; Linux ttyAMA1 is connected to its UART (ttyAMA0 remains the host console)."
  echo "In Linux: erbctl console /dev/ttyAMA1 (Ctrl-] exits). See docs/uart-console.md for attach/load steps."
  "$R/scripts/run-linux.sh" "${ARGS[@]}" <&0 &
  QEMU_PID=$!
  rc=0
  wait "$QEMU_PID" || rc=$?
  QEMU_PID=
  [ "$rc" = 0 ] || exit "$rc"
else
  echo "== Guest UART1 ↔ Erbium UART test (empty MRAM, no -elf, independent xSPI control)"
  QEMU_GROUP=1
  timeout -k 2 180 "$R/scripts/run-linux.sh" "${ARGS[@]}" "${MODE[@]}" < /dev/null > "$T/guest.log" 2>&1 &
  QEMU_PID=$!
  rc=0
  wait "$QEMU_PID" || rc=$?
  QEMU_PID=
  if [ "$rc" != 0 ]; then
    cat "$T/guest.log" "$T/backend.log" >&2
    exit 1
  fi
  cat "$T/guest.log"
  if ! tr -d '\r' < "$T/guest.log" | grep -qx "$MARKER 0"; then
    cat "$T/backend.log" >&2
    exit 1
  fi
fi
