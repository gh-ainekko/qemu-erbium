#!/usr/bin/env bash
# Read-only checks; paths are relative to this script, never the caller's cwd.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
mode=${1:-deps}
target=${2:-all}
case "$mode" in checkout|deps|fetch|build) ;; *) echo "usage: $0 [checkout|deps|fetch|build] [all|qemu|sysemu|firmware|linux]" >&2; exit 2;; esac
case "$target" in all|qemu|sysemu|firmware|linux) ;; *) echo "Unknown build target: $target" >&2; exit 2;; esac
errors=0
fail() { echo "preflight: ERROR: $*" >&2; errors=$((errors + 1)); }
file() { [ -s "$R/$1" ] || fail "Missing or empty file: $R/$1"; }
tool() { command -v "$1" >/dev/null 2>&1 || fail "Missing command: $1 (Ubuntu package: $2)"; }
selected() { [ "$target" = all ] || [ "$target" = "$1" ]; }
for f in bootstrap.sh scripts/env.sh scripts/preflight.sh scripts/fetch-sources.sh scripts/sync-git-patches.sh scripts/build-all.sh scripts/configure-linux.sh scripts/build-load-test.sh scripts/run-load-test.sh scripts/run-e2e.sh scripts/run-linux.sh linux/erbium.config linux/initramfs.list linux/rootfs/init linux/rootfs/erbium-test.sh linux/rootfs/erbium-load-test.sh linux/firmware/loader-smoke.S linux/firmware/loader-smoke.ld linux/tools/erbctl.c linux/tools/erbium-loader.c linux/tools/erbium-loader.h linux/tools/erbium-xspi.h; do file "$f"; done
shopt -s nullglob
for dir in qemu-patches sysemu-patches linux/patches; do
  patches=("$R/$dir/"*.patch)
  if [ ${#patches[@]} -eq 0 ]; then fail "No patch files in $R/$dir"; else
    for p in "${patches[@]}"; do file "${p#"$R/"}"; done
  fi
done
if [ "$errors" -ne 0 ]; then
  echo 'preflight: Incomplete source checkout. Clone the full qemu-erbium repository, not just bootstrap.sh or the binary release. Restore missing tracked files before retrying.' >&2
  exit 1
fi
if [ "$mode" = checkout ]; then echo "preflight: checkout OK ($R)"; exit 0; fi
for pair in 'git:git' 'curl:curl' 'patch:patch' 'tar:tar' 'xz:xz-utils' 'dpkg-deb:dpkg'; do tool "${pair%%:*}" "${pair#*:}"; done
if [ "$mode" != fetch ]; then
  for pair in 'gcc:build-essential' 'g++:build-essential' 'make:build-essential' 'pkg-config:pkg-config' 'python3:python3'; do tool "${pair%%:*}" "${pair#*:}"; done
  modules=()
  if selected qemu; then
    tool ninja ninja-build; tool flex flex; tool bison bison
    tool libgcrypt-config libgcrypt20-dev
    modules+=(glib-2.0 pixman-1 slirp zlib)
    if command -v python3 >/dev/null; then
      python3 -c 'import venv, ensurepip' 2>/dev/null || fail 'Python venv/ensurepip missing (Ubuntu package: python3-venv)'
    fi
  fi
  if selected sysemu; then tool cmake cmake; tool ninja ninja-build; modules+=(liblz4); fi
  if selected firmware; then tool riscv64-unknown-elf-gcc gcc-riscv64-unknown-elf; fi
  if selected linux; then
    for pair in 'aarch64-linux-gnu-gcc:gcc-aarch64-linux-gnu' 'riscv64-unknown-elf-gcc:gcc-riscv64-unknown-elf' 'riscv64-unknown-elf-ld:binutils-riscv64-unknown-elf' 'riscv64-unknown-elf-readelf:binutils-riscv64-unknown-elf' 'flex:flex' 'bison:bison' 'bc:bc' 'cpio:cpio'; do tool "${pair%%:*}" "${pair#*:}"; done
    modules+=(openssl libelf)
    if command -v aarch64-linux-gnu-gcc >/dev/null; then
      printf 'int main(void) {return 0;}\n' | aarch64-linux-gnu-gcc -static -x c -o /dev/null - 2>/dev/null || fail 'Cannot link static arm64 executable (install libc6-dev-arm64-cross and gcc-aarch64-linux-gnu)'
    fi
  fi
  if command -v pkg-config >/dev/null; then
    for module in "${modules[@]}"; do pkg-config --exists "$module" || fail "Missing development library: $module (rerun ./bootstrap.sh without SKIP_APT)"; done
  fi
  if command -v gcc >/dev/null && selected qemu; then
    printf '#include <libfdt.h>\nint main(void){return fdt_check_header(0);}\n' | gcc -x c - -o /dev/null -lfdt 2>/dev/null || fail 'Missing libfdt headers/library (Ubuntu package: libfdt-dev)'
  fi
  if command -v g++ >/dev/null && selected sysemu; then
    printf '#include <glog/logging.h>\nint main(){google::InitGoogleLogging("check");}\n' | g++ -x c++ - -o /dev/null -lglog 2>/dev/null || fail 'Missing glog headers/library (Ubuntu package: libgoogle-glog-dev)'
  fi
fi
if [ "$mode" = build ]; then
  if selected qemu; then file ext/qemu/configure; file ext/qemu/hw/ssi/erbium-xspi.c; fi
  if selected sysemu; then file et-platform/erbium-hal/CMakeLists.txt; file et-platform/sw-sysemu/CMakeLists.txt; fi
  if selected firmware; then file et-platform/sw-sysemu/tests/erbium/host/mailbox_worker.c; fi
  if selected linux; then
    file ext/linux/Makefile; file ext/linux/scripts/config; file ext/linux/drivers/mtd/devices/erbium-xspi.c; file ext/linux/include/uapi/linux/erbium-xspi.h; file linux/rootfs/busybox
  fi
fi
if [ "$errors" -ne 0 ]; then
  echo "preflight: $errors problem(s). Run ./bootstrap.sh to install dependencies and fetch sources; no build was started." >&2
  exit 1
fi
echo "preflight: $mode checks OK ($target)"
