#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Repeatable #1/#2 checkpoints. Does not flash a board or run the IDE installer.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
B=${SMALLVM_BUILD:-"$R/build/smallvm"}
host_only=0; legacy=0
for arg in "$@"; do
  case "$arg" in
    --host-only) host_only=1 ;;
    --with-legacy32) legacy=1 ;;
    *) echo "usage: $0 [--host-only] [--with-legacy32]" >&2; exit 2 ;;
  esac
done
"$R/scripts/fetch-smallvm.sh"
mkdir -p "$B/results"
python3 "$R/smallvm/tests/test_fetch.py" 2>&1 | tee "$B/results/fetch-safety.log"
make -C "$R/smallvm" BUILD="$B" host-selftest host-selftest-sanitize 2>&1 | tee "$B/results/host.log"
if ((legacy)); then
  make -C "$R/smallvm" BUILD="$B" host-selftest-32 2>&1 | tee "$B/results/legacy32.log"
fi
if ((!host_only)); then
  make -C "$R/smallvm" BUILD="$B" firmware selftest 2>&1 | tee "$B/results/rv64-build.log"
  for image in smallvm smallvm-selftest; do
    python3 "$R/smallvm/tests/check_elf.py" "$B/$image.elf" | tee "$B/results/$image-elf.json"
  done
  EMU=${ERBIUM_EMU:-"$R/dist/bin/erbium_emu"}
  if [[ ! -x "$EMU" && -z "${ERBIUM_EMU:-}" ]]; then EMU="$R/build/sw-sysemu/erbium_emu"; fi
  [[ -x "$EMU" ]] || { echo 'Build the emulator first: scripts/build-all.sh sysemu' >&2; exit 1; }
  run=(python3 "$R/smallvm/tests/run_emulator.py" --emu "$EMU")
  "${run[@]}" "$B/smallvm-selftest.elf" --output "$B/results/rv64"
  "${run[@]}" "$B/smallvm-selftest.elf" --output "$B/results/rv64-poison" --poison-mram --minions 0xff
  "${run[@]}" "$B/smallvm.elf" --output "$B/results/protocol" --mode protocol
  "${run[@]}" "$B/smallvm-selftest.elf" --output "$B/results/fault" --mode fault
  "${run[@]}" "$B/smallvm-selftest.elf" --output "$B/results/no-bss" --mode no-bss
  # Negative control proves timer test is not just comparing the clock with itself.
  make -C "$R/smallvm" BUILD="$B/bad-timer" ERBIUM_TIMER_HZ=10000000 selftest > "$B/results/bad-timer-build.log" 2>&1
  if "${run[@]}" "$B/bad-timer/smallvm-selftest.elf" --output "$B/results/bad-timer"; then
    echo 'FAIL: incorrect timer calibration was accepted' >&2; exit 1
  fi
  grep -q 'FAIL timer-calibration' "$B/results/bad-timer/uart.txt"
  echo 'PASS wrong-timer-calibration-rejected'
fi
python3 - "$B" "${SMALLVM_SRC:-$R/ext/smallvm}" "$R" <<'PY'
import hashlib, json, pathlib, subprocess, sys
b, src, root = map(pathlib.Path, sys.argv[1:])
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
manifest = {'smallvm_commit': subprocess.check_output(['git','-C',str(src),'rev-parse','HEAD'],text=True).strip(),
            'source_tree': subprocess.check_output(['git','-C',str(src),'rev-parse','HEAD^{tree}'],text=True).strip(),
            'patches': {p.name: sha(p) for p in sorted((root/'smallvm/patches').glob('*.patch'))},
            'artifacts': {p.name: sha(p) for p in b.iterdir() if p.is_file() and p.suffix in ('.elf','.map','.dis','.headers')},
            'results': {str(p.relative_to(b)): sha(p) for p in sorted((b/'results').rglob('*')) if p.is_file() and p.name != 'manifest.json' and p.suffix != '.img'}}
(b/'results/manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
PY
echo "PASS SmallVM checkpoints; results: $B/results"
