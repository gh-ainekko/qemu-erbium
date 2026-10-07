#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build the *real* pinned MicroBlocks GP compiler and IDE for the browser.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
SRC=${SMALLVM_SRC:-"$R/ext/smallvm"}
OUT=${SMALLVM_WEB_BUILD:-"$R/build/smallvm/web"}
"$R/scripts/fetch-smallvm.sh"
command -v emcc >/dev/null || {
  echo 'Install Emscripten 3.1.6 and clang/LLVM/lld 14 (see smallvm/web/README.md).' >&2
  exit 1
}
# Ubuntu's distro cache is root-owned/frozen. Seed a writable project cache;
# never change /usr/share or require sudo for the actual build.
export EM_CACHE=${EM_CACHE:-"$R/build/smallvm/emscripten-cache"}
if [[ ! -e "$EM_CACHE/sysroot_install.stamp" && -d /usr/share/emscripten/cache ]]; then
  mkdir -p "$EM_CACHE"
  cp -a /usr/share/emscripten/cache/. "$EM_CACHE/"
fi
# This older emcc treats environment values as strings; empty means false.
export EM_FROZEN_CACHE=
# Emscripten 3.1.6 expects clang 14's "main" symbol. Ubuntu's clang 15
# rewrites argc/argv main to __main_argc_argv, silently dropping the GP entry.
[[ -x /usr/lib/llvm-14/bin/clang ]] || {
  echo 'Install clang-14 llvm-14 lld-14 (see smallvm/web/README.md).' >&2; exit 1;
}
export EM_LLVM_ROOT=/usr/lib/llvm-14/bin
export EM_LLVM_ADD_VERSION= EM_CLANG_ADD_VERSION=
# GP's version primitive embeds __DATE__/__TIME__. Fix them to the base
# commit timestamp so independently staged builds have identical hashes.
export SOURCE_DATE_EPOCH=1791323672
# Git tree, rather than git-am commit identity, locks the patched source.
TREE=73844f74e19e41eb466ea0c727a85594902be4c1
[[ $(git -C "$SRC" rev-parse HEAD^{tree}) == "$TREE" ]] || {
  echo "SmallVM web source must match pinned patched tree $TREE" >&2; exit 1;
}
STAGE=$(mktemp -d)
trap 'rmdir "$STAGE" 2>/dev/null || true' EXIT
# Export tracked files only; never run the destructive upstream build in ext/.
git -C "$SRC" archive "$TREE" chromeApp gp/runtime ide Examples Libraries translations img \
  LICENSE 'Mozilla Public License, version 2.0.html' | tar -x -C "$STAGE"
python3 "$R/smallvm/web/build.py" "$STAGE" "$OUT"
echo "Self-hosted MicroBlocks web assets: $OUT"
# Stage is intentionally retained for source inspection and build diagnostics.
echo "Pinned build source: $STAGE"
