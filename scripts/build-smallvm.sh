#!/usr/bin/env bash
# Build the opt-in SmallVM target; no Arduino, no system libc installation.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
if [[ ${1:-} == --prepare-libc ]]; then
  D=${SMALLVM_DEPS:-"$R/build/smallvm/deps"}
  P="$D/usr/lib/picolibc/riscv64-unknown-elf"
  SHA=9563bbe39bbdf4eda1970ced8c54e5df47a112d91427fc4fc38100428fd23a9b
  URL=https://archive.ubuntu.com/ubuntu/pool/universe/p/picolibc/picolibc-riscv64-unknown-elf_1.8.6-2_all.deb
  mkdir -p "$D"
  if [[ ! -f "$D/picolibc.deb" ]]; then
    curl --fail --location --retry 3 -o "$D/picolibc.deb.download" "$URL"
    printf '%s  %s\n' "$SHA" "$D/picolibc.deb.download" | sha256sum --check
    mv "$D/picolibc.deb.download" "$D/picolibc.deb"
  fi
  printf '%s  %s\n' "$SHA" "$D/picolibc.deb" | sha256sum --check
  # Select only RV64IM softfloat LP64 libc and common headers (~7MB).
  ar p "$D/picolibc.deb" data.tar.zst | tar --zstd -xf - -C "$D" \
    ./usr/lib/picolibc/riscv64-unknown-elf/include \
    ./usr/lib/picolibc/riscv64-unknown-elf/lib/rv64im/lp64/libc.a
  test -f "$P/include/stdio.h" && test -f "$P/lib/rv64im/lp64/libc.a"
  printf 'Ubuntu picolibc-riscv64-unknown-elf 1.8.6-2\nsha256=%s\n' "$SHA" > "$D/picolibc.lock"
  exit 0
fi
exec make -C "$R/smallvm" "${@:-firmware}"
