#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Opt-in pinned source; safely append only a verified patch-series suffix.
set -euo pipefail
export GIT_OPTIONAL_LOCKS=0
R=$(cd "$(dirname "$0")/.." && pwd)
SRC=${SMALLVM_SRC:-"$R/ext/smallvm"}
BASE=49f337529294640def7b27a2ee1c4e7ba88ccdbb
URL=${SMALLVM_URL:-https://codeberg.org/MicroBlocks/smallvm.git}
shopt -s nullglob
patches=("$R"/smallvm/patches/*.patch)
((${#patches[@]})) || { echo 'No SmallVM patches found' >&2; exit 1; }
# Preserve the SmallVM wrapper's stricter policy for untracked user work too.
if [[ -d "$SRC/.git" ]] && [[ -n "$(git -C "$SRC" status --porcelain)" ]]; then
  echo "Refusing to overwrite dirty source: $SRC" >&2
  echo 'Commit/stash your work yourself, or choose a fresh SMALLVM_SRC directory.' >&2
  exit 1
fi
"$R/scripts/sync-git-patches.sh" "$SRC" "$URL" "$BASE" erbium-port "$R/smallvm/patches"
digest=$(cat "${patches[@]}" | sha256sum | cut -d' ' -f1)
printf '%s %s\n' "$digest" "$(git -C "$SRC" rev-parse HEAD^{tree})" > "$SRC/.git/erbium-source-lock"
echo "SmallVM ready: $SRC ($(git -C "$SRC" rev-parse --short HEAD))"
