#!/usr/bin/env bash
# Expand checkout-local paths BEFORE Kconfig reads them, then refresh generated
# configuration too (an earlier attempt may have cached the literal @R@ path).
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$R/build/linux"
python3 - "$R" <<'PY'
from pathlib import Path
import sys
import os
import shutil
root = Path(sys.argv[1])
for name in ('erbium.config', 'initramfs.list'):
    text = (root / 'linux' / name).read_text().replace('@R@', str(root))
    (root / 'build' / name).write_text(text)
# Optional user ELF for interactive host loading; it is not preloaded into MRAM.
# Copy to a stable build path so initramfs filenames never contain user whitespace.
if os.environ.get('ERBIUM_ELF'):
    source = Path(os.environ['ERBIUM_ELF']).resolve(strict=True)
    target = root / 'build/host-payload.elf'
    if not source.is_file():
        sys.exit(f'ERBIUM_ELF must name a regular file: {source}')
    if source != target:
        shutil.copyfile(source, target)
    with (root / 'build/initramfs.list').open('a') as out:
        out.write(f'file /firmware/host-payload.elf {target} 0644 0 0\n')
PY
MK=(make -C "$R/ext/linux" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- O="$R/build/linux")
if [ ! -f "$R/build/linux/.config" ]; then
  "${MK[@]}" KCONFIG_ALLCONFIG="$R/build/erbium.config" allnoconfig
fi
# Preserve existing configuration while repairing the path and enabling the
# advisory locks required by erbctl, including in pre-existing minimal builds.
"$R/ext/linux/scripts/config" --file "$R/build/linux/.config" \
  --set-str INITRAMFS_SOURCE "$R/build/initramfs.list" --enable FILE_LOCKING
"${MK[@]}" olddefconfig
"${MK[@]}" syncconfig
python3 - "$R" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
expected = str(root / "build/initramfs.list")
for name in ('.config', 'include/config/auto.conf'):
    path = root / 'build/linux' / name
    values = [line.partition("=")[2].strip('"') for line in path.read_text().splitlines()
              if line.startswith("CONFIG_INITRAMFS_SOURCE=")]
    if values != [expected]:
        sys.exit(f'Invalid INITRAMFS_SOURCE in {path}; stopping before compilation')
PY
