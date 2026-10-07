#!/usr/bin/env bash
# Boot the erbium guest kernel on xlnx-versal-virt with the erbium-xspi device.
#   scripts/run-linux.sh [--backend SOCK] [--mram FILE] [--uart-socket SOCK] [--test | --load-test | --uart-test | --kotama-test] [--share DIR | --share-rw DIR] [extra qemu args]
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd); . "$R/scripts/env.sh"
MRAM=/tmp/mram.img; SOCK=""; UART=""; MODE=""; APPEND="console=ttyAMA0 earlycon"; EXTRA=(); SHARE_DIR=""; SHARE_MODE=""
need_value() { [ "$#" -ge 2 ] && [ -n "$2" ] || { echo "Missing value for $1" >&2; exit 2; }; }
set_mode() { [ -z "$MODE" ] || { echo "Select only one guest test mode" >&2; exit 2; }; MODE=$1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --share|--share-rw)
      need_value "$@"
      [ -z "$SHARE_MODE" ] || { echo "Select only one shared directory" >&2; exit 2; }
      SHARE_MODE=ro; [ "$1" != --share-rw ] || SHARE_MODE=rw
      SHARE_DIR=$2; shift 2;;
    --backend) need_value "$@"; SOCK=$2; shift 2;;
    --mram) need_value "$@"; MRAM=$2; shift 2;;
    --uart-socket) need_value "$@"; UART=$2; shift 2;;
    --load-test) set_mode loadtest; shift;;
    --uart-test) set_mode uarttest; shift;;
    --kotama-test) set_mode kotamatest; shift;;
    --test) set_mode autotest; shift;;
    *) EXTRA+=("$1"); shift;;
  esac
done
if [ "$MODE" = uarttest ] || [ "$MODE" = kotamatest ]; then
  [ -n "$SOCK" ] && [ -n "$UART" ] || { echo "UART guest tests require --backend and --uart-socket" >&2; exit 2; }
fi
[ -z "$MODE" ] || APPEND="$APPEND erbium.$MODE erbium.poweroff"
# Commas delimit QEMU chardev options, so reject ambiguous path spelling.
[[ "$SOCK$UART" != *,* ]] || { echo "Socket paths must not contain commas" >&2; exit 2; }
prepare_share
[ -z "$SHARE_CMDLINE" ] || APPEND="$APPEND $SHARE_CMDLINE"
require_guest
[ -f "$MRAM" ] || truncate -s 16M "$MRAM"
CHR=()
if [ -n "$SOCK" ]; then
  CHR=(-chardev socket,id=erb,path="$SOCK" -global erbium-xspi.chardev=erb)
  APPEND="$APPEND erbium.backend"
fi
SERIAL=(-serial mon:stdio)
if [ -n "$UART" ]; then
  SERIAL+=(-chardev "socket,id=erb-uart,path=$UART,reconnect-ms=1000" -serial chardev:erb-uart)
fi
exec "$QEMU" "${QEMU_ARGS[@]}" \
  -M xlnx-versal-virt,ospi-flash=erbium-xspi -m 1G \
  -object memory-backend-file,id=mram,size=16M,mem-path="$MRAM",share=on \
  -global erbium-xspi.memdev=mram \
  -global driver=xlnx.versal-ospi,property=faithful-frames,value=on \
  "${CHR[@]}" "${SHARE_ARGS[@]}" \
  -kernel "$IMAGE" \
  -append "$APPEND" \
  -display none "${SERIAL[@]}" -no-reboot "${EXTRA[@]}"
