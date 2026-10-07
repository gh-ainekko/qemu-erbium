#!/usr/bin/env bash
# Standalone MRAM fixture only; parent Linux build compiles the guest C client.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
CROSS_COMPILE=${RISCV_CROSS_COMPILE:-riscv64-unknown-elf-}
mkdir -p "$R/build"
"${CROSS_COMPILE}gcc" -c -nostdlib -march=rv64imc_zicsr -mabi=lp64 \
    -mcmodel=medany -mno-relax -Wall -Wextra -Werror \
    -o "$R/build/uart-smoke.o" "$R/linux/firmware/uart-smoke.S"
"${CROSS_COMPILE}ld" -m elf64lriscv --no-relax \
    -T "$R/linux/firmware/uart-smoke.ld" \
    -o "$R/build/uart-smoke.elf" "$R/build/uart-smoke.o"
"${CROSS_COMPILE}readelf" -h -l "$R/build/uart-smoke.elf"
echo "Built $R/build/uart-smoke.elf"
