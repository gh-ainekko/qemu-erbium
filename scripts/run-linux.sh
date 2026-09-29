#!/usr/bin/env bash
# Boot the erbium guest kernel on xlnx-versal-virt with the erbium-xspi device.
#   scripts/run-linux.sh [--backend SOCK] [--mram FILE] [--test] [extra qemu args]
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
MRAM=/tmp/mram.img; SOCK=""; APPEND="console=ttyAMA0 earlycon"; EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --backend) SOCK=$2; shift 2;;
    --mram) MRAM=$2; shift 2;;
    --test) APPEND="$APPEND erbium.autotest erbium.poweroff"; shift;;
    *) EXTRA+=("$1"); shift;;
  esac
done
[ -f "$MRAM" ] || truncate -s 16M "$MRAM"
CHR=()
if [ -n "$SOCK" ]; then
  CHR=(-chardev socket,id=erb,path="$SOCK" -global erbium-xspi.chardev=erb)
  APPEND="$APPEND erbium.backend"
fi
exec "$R/ext/qemu/build/qemu-system-aarch64" \
  -M xlnx-versal-virt,ospi-flash=erbium-xspi -m 1G \
  -object memory-backend-file,id=mram,size=16M,mem-path="$MRAM",share=on \
  -global erbium-xspi.memdev=mram \
  -global driver=xlnx.versal-ospi,property=faithful-frames,value=on \
  "${CHR[@]}" \
  -kernel "$R/build/linux/arch/arm64/boot/Image" \
  -append "$APPEND" \
  -display none -serial mon:stdio -no-reboot "${EXTRA[@]}"
