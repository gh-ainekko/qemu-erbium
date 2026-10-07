#!/usr/bin/env bash
# Fetch a pinned Git tree or append the missing suffix of its patch series.
# Existing histories are inspected only: never reset, rebase, or delete them.
set -euo pipefail
export LC_ALL=C
# Inspection (especially git status) must not refresh the user's index on disk.
export GIT_OPTIONAL_LOCKS=0

if [ "$#" -ne 5 ]; then
  echo "usage: $0 DIR URL PINNED_COMMIT BRANCH PATCH_DIR" >&2
  exit 2
fi
dir=$1 url=$2 sha=$3 branch=$4 patchdir=$5
# git am resolves filenames inside the source checkout, not the caller's cwd.
[[ "$patchdir" = /* ]] || patchdir="$PWD/$patchdir"
fail() { echo "$dir: ERROR: $*" >&2; exit 1; }
g() { git --no-replace-objects -C "$dir" "$@"; }
patch_id() {
  local ids
  ids=$(git -c patchid.verbatim=false patch-id --stable) || return 1
  # A file must contain exactly one nonempty patch, not an mbox series.
  [ -n "$ids" ] && [[ "$ids" != *$'\n'* ]] || return 1
  printf '%s\n' "${ids%% *}"
}

shopt -s nullglob
patches=("$patchdir/"*.patch)
[ "${#patches[@]}" -gt 0 ] || fail "No patches in $patchdir."
patch_ids=()
for patch in "${patches[@]}"; do
  id=$(patch_id < "$patch") || fail "Cannot identify a single patch in $patch."
  patch_ids+=("$id")
done

if [ ! -e "$dir/.git" ]; then
  # Do not adopt a nonempty directory (possibly an interrupted checkout).
  if [ -e "$dir" ] && { [ ! -d "$dir" ] || [ -n "$(ls -A "$dir")" ]; }; then
    fail "Not a Git checkout or empty directory. Inspect this path and use a separate fresh source directory; nothing was removed."
  fi
  mkdir -p "$dir"
  g init -q
  g remote add origin "$url"
  g fetch -q --depth 1 origin "$sha"
  g checkout -q -b "$branch" FETCH_HEAD
fi

top=$(g rev-parse --show-toplevel 2>/dev/null) || fail "Invalid Git working tree; inspect it and fetch into a separate fresh directory."
[ "$top" = "$(cd "$dir" && pwd -P)" ] || fail "Expected a source checkout rooted at $dir, not $top."
head=$(g rev-parse --verify HEAD^{commit} 2>/dev/null) ||
  fail "Git checkout has no HEAD (possibly an interrupted fetch). Inspect it and fetch into a separate fresh directory; no repair was attempted."
for state in rebase-apply rebase-merge MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD; do
  state_path=$(g rev-parse --path-format=absolute --git-path "$state")
  [ ! -e "$state_path" ] || fail "Git operation in progress ($state). Finish or abort it yourself before retrying."
done
tracked_status=$(g status --porcelain --untracked-files=no --ignore-submodules=untracked) ||
  fail "Cannot inspect the tracked working tree/index; refusing to apply patches."
[ -z "$tracked_status" ] ||
  fail "Tracked working tree/index is dirty. Commit or stash your changes before retrying; nothing was changed."
base=$(g rev-parse --verify "$sha^{commit}" 2>/dev/null) ||
  fail "Expected pinned base $sha is missing. Inspect this checkout and use a separate fresh source directory."
g merge-base --is-ancestor "$base" "$head" ||
  fail "HEAD is not based on expected pinned base $sha; refusing to rewrite existing history."

# Walk every commit, not just first parents: merges, extras, changed/reordered
# patches and different bases must all be rejected before any git am.
history=$(g rev-list --reverse "$base..$head")
commits=()
if [ -n "$history" ]; then mapfile -t commits <<< "$history"; fi
[ "${#commits[@]}" -le "${#patches[@]}" ] ||
  fail "History has extra/custom commits after pinned base; expected an exact patch-series prefix."
previous=$base
for i in "${!commits[@]}"; do
  commit=${commits[$i]}
  [ "$(g rev-list --parents -n 1 "$commit")" = "$commit $previous" ] ||
    fail "Nonlinear/custom history at $commit; expected an exact patch-series prefix."
  id=$(g -c diff.renames=true show --format= --binary --no-ext-diff --no-textconv \
    --no-color --no-relative --src-prefix=a/ --dst-prefix=b/ --unified=3 \
    --inter-hunk-context=0 --diff-algorithm=myers --indent-heuristic "$commit" | patch_id) ||
    fail "Cannot identify patch for commit $commit; expected an exact patch-series prefix."
  [ "$id" = "${patch_ids[$i]}" ] ||
    fail "Patch-series prefix mismatch at commit $commit (${patches[$i]##*/}). Custom, changed, or reordered patches require manual review; nothing was changed."
  previous=$commit
done

applied=${#commits[@]}
if [ "$applied" -eq "${#patches[@]}" ]; then
  echo "$dir: patch series already current ($applied patches)"
  exit 0
fi
echo "$dir: applying $((${#patches[@]} - applied)) missing patches ($applied already present)"
# Identity is command-local; never write the user's repository/global config.
# If application fails, leave Git's diagnostic/state for manual inspection.
if ! g -c user.name=erbium-bootstrap -c user.email=bootstrap@erbium.local \
  -c commit.gpgSign=false \
  am -q "${patches[@]:$applied}"; then
  fail "git am failed. Inspect the checkout and use git am --continue or git am --abort yourself; no history was reset."
fi
echo "$dir: patch series current (${#patches[@]} patches)"
