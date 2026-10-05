#!/usr/bin/env bash
# One-shot: install Ubuntu deps, fetch pinned sources + apply patches, build, run the e2e test.
# Tested on Ubuntu 24.04 (2 vCPU: ~45 min; GitHub runner: ~25 min).
set -euo pipefail
R=$(cd "$(dirname "$0")" && pwd)
"$R/scripts/preflight.sh" checkout
if [ -z "${SKIP_APT:-}" ]; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    build-essential git curl patch xz-utils libc6-dev-arm64-cross ninja-build meson pkg-config flex bison python3-venv python3-pip ccache \
    libglib2.0-dev libpixman-1-dev libfdt-dev libslirp-dev zlib1g-dev libgcrypt20-dev \
    cmake libgoogle-glog-dev liblz4-dev lz4 \
    gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf \
    gcc-aarch64-linux-gnu libssl-dev libelf-dev bc cpio kmod dwarves device-tree-compiler
fi
"$R/scripts/preflight.sh" deps
"$R/scripts/fetch-sources.sh"
"$R/scripts/build-all.sh"
"$R/scripts/run-e2e.sh"
