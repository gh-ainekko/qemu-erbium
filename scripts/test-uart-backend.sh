#!/usr/bin/env bash
# Source-only register, endpoint lifecycle and real CPU IRQ/WFI regressions.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
EMU=${EMU:-"$R/build/sw-sysemu/erbium_emu"}
[ -x "$EMU" ] || { echo "Build the backend first: J=2 scripts/build-all.sh sysemu" >&2; exit 1; }
T=$(mktemp -d /tmp/erbium-uart-unit.XXXXXX)
trap 'rm -rf "$T"' EXIT
bash "$R/et-platform/sw-sysemu/tests/erbium/uart-registers/run.sh"
"${CXX:-c++}" -std=c++17 -O1 -g -Wall -Wextra -Werror -pedantic \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I"$R/et-platform/sw-sysemu/sys_emu" \
  "$R/et-platform/sw-sysemu/sys_emu/uart_socket.cpp" \
  "$R/et-platform/sw-sysemu/tests/erbium/host/test_uart_socket.cpp" -o "$T/lifecycle"
ASAN_OPTIONS=halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 timeout 30 "$T/lifecycle"
python3 "$R/et-platform/sw-sysemu/tests/erbium/host/test_uart_endpoint.py" --emu "$EMU"
UART_IRQ_TEST_EMU="$EMU" python3 "$R/tests/test_uart_irq_fixture.py"
