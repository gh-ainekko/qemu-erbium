#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Focused native checks; --emulator also builds/runs a real MMIO echo firmware.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${UART_TRANSPORT_BUILD:-"$ROOT/build/smallvm/uart-transport"}
mkdir -p "$OUT"
"${CXX:-c++}" -std=c++17 -O1 -g -Wall -Wextra -Werror -DERBIUM \
  -fsanitize=address,undefined -fno-omit-frame-pointer -pthread \
  -I"$ROOT/et-platform/sw-sysemu" -I"$ROOT/et-platform/erbium-hal/include" \
  "$ROOT/smallvm/tests/uart_transport_device.cpp" \
  -Wl,--wrap=write -Wl,--wrap=read -o "$OUT/device-test"
ASAN_OPTIONS=halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 timeout 10 "$OUT/device-test" | tee "$OUT/device-test.log"
if [ "${1:-}" = --emulator ]; then
  riscv64-unknown-elf-gcc -march=rv64imc_zicsr_zifencei -mabi=lp64 -mcmodel=medany \
    -O2 -g -Wall -Wextra -Werror -ffreestanding -fno-stack-protector \
    -nostdlib -Wl,--no-relax -T"$ROOT/smallvm/erbium/erbium.ld" \
    "$ROOT/smallvm/erbium/startup.S" "$ROOT/smallvm/tests/uart_transport_echo.c" \
    -o "$OUT/echo.elf"
  python3 "$ROOT/smallvm/tests/uart_transport_emulator.py" "$OUT/echo.elf" \
    --emu "${ERBIUM_EMU:-$ROOT/dist/bin/erbium_emu}" --output "$OUT/emulator"
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--emulator]" >&2
  exit 2
fi
