#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Opt-in, pinned SmallVM source + reviewable patch series; never alter a dirty tree.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
SRC=${SMALLVM_SRC:-"$R/ext/smallvm"}
BASE=49f337529294640def7b27a2ee1c4e7ba88ccdbb
URL=${SMALLVM_URL:-https://codeberg.org/MicroBlocks/smallvm.git}
shopt -s nullglob
patches=("$R"/smallvm/patches/*.patch)
((${#patches[@]})) || { echo 'No SmallVM patches found' >&2; exit 1; }
digest=$(cat "${patches[@]}" | sha256sum | cut -d' ' -f1)
if [ -e "$SRC" ]; then
  if [ -d "$SRC/.git" ] && [ -f "$SRC/.git/erbium-source-lock" ]; then
    read -r old_digest old_tree < "$SRC/.git/erbium-source-lock"
    if [ "$old_digest" = "$digest" ] && [ "$old_tree" = "$(git -C "$SRC" rev-parse HEAD^{tree})" ] && [ -z "$(git -C "$SRC" status --porcelain)" ]; then
      echo "SmallVM ready: $SRC ($(git -C "$SRC" rev-parse --short HEAD))"; exit 0
    fi
  fi
  echo "Refusing to overwrite unverified, dirty, or differently patched source: $SRC" >&2
  echo 'Choose a fresh SMALLVM_SRC directory or preserve/move the existing checkout first.' >&2
  exit 1
fi
mkdir -p "$(dirname "$SRC")"
tmp=$(mktemp -d "${SRC}.fetch.XXXXXX")
trap 'if [ -n "${tmp:-}" ]; then echo "Incomplete fetch retained at $tmp" >&2; fi' EXIT
git -C "$tmp" init -q
git -C "$tmp" remote add origin "$URL"
git -C "$tmp" fetch -q --depth 1 origin "$BASE"
git -C "$tmp" checkout -q -b erbium-port FETCH_HEAD
git -C "$tmp" -c user.name=erbium-bootstrap -c user.email=bootstrap@erbium.local am -q "${patches[@]}"
printf '%s %s\n' "$digest" "$(git -C "$tmp" rev-parse HEAD^{tree})" > "$tmp/.git/erbium-source-lock"
mv "$tmp" "$SRC"; tmp=
echo "SmallVM fetched and patched: $SRC"
