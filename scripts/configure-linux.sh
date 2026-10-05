#!/usr/bin/env bash
# Expand checkout-local paths BEFORE Kconfig reads them, then refresh generated
# configuration too (an earlier attempt may have cached the literal @R@ path).
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$R/build/linux"
python3 - "$R" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
for name in ('erbium.config', 'initramfs.list'):
    text = (root / 'linux' / name).read_text().replace('@R@', str(root))
    (root / 'build' / name).write_text(text)
PY
MK=(make -C "$R/ext/linux" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- O="$R/build/linux")
if [ ! -f "$R/build/linux/.config" ]; then
  "${MK[@]}" KCONFIG_ALLCONFIG="$R/build/erbium.config" allnoconfig
fi
# Preserve existing configuration while repairing the path on every invocation.
"$R/ext/linux/scripts/config" --file "$R/build/linux/.config" \
  --set-str INITRAMFS_SOURCE "$R/build/initramfs.list"
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
