#!/usr/bin/env bash
# Re-apply the erbium patch series onto fresh clones of Xilinx QEMU and et-platform.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
(cd "$R/ext/qemu" && git checkout -q -b erbium 2>/dev/null || git checkout -q erbium; git am "$R"/qemu-patches/*.patch)
(cd "$R/et-platform" && git checkout -q -b erbium-qemu-backend 2>/dev/null || git checkout -q erbium-qemu-backend; git am "$R"/sysemu-patches/*.patch)
