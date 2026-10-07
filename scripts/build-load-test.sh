#!/usr/bin/env bash
# Build only the standalone MRAM loader fixture; no et-platform dependency.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
CROSS_COMPILE=${RISCV_CROSS_COMPILE:-riscv64-unknown-elf-}
mkdir -p "$R/build"
"${CROSS_COMPILE}gcc" -c -nostdlib -march=rv64imc -mabi=lp64 \
    -mcmodel=medany -mno-relax -Wall -Wextra -Werror \
    -o "$R/build/loader-smoke.o" "$R/linux/firmware/loader-smoke.S"
"${CROSS_COMPILE}ld" -m elf64lriscv --no-relax \
    -T "$R/linux/firmware/loader-smoke.ld" \
    -o "$R/build/loader-smoke.elf" "$R/build/loader-smoke.o"
"${CROSS_COMPILE}readelf" -h -l "$R/build/loader-smoke.elf"
echo "Built $R/build/loader-smoke.elf"
