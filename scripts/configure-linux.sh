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
# Upgrade pre-existing minimal builds as well as fresh ones. 9P's default FD
# transport implies INET/UNIX; disable it so virtio sharing needs no IP stack.
# Preserve other user configuration while repairing the path and required features.
"$R/ext/linux/scripts/config" --file "$R/build/linux/.config" \
  --set-str INITRAMFS_SOURCE "$R/build/initramfs.list" --enable FILE_LOCKING \
  --enable NET --enable NET_9P --disable NET_9P_FD --enable NET_9P_VIRTIO \
  --enable NETWORK_FILESYSTEMS --enable 9P_FS \
  --enable VIRTIO_MENU --enable VIRTIO --enable VIRTIO_MMIO
"${MK[@]}" olddefconfig
"${MK[@]}" syncconfig
python3 - "$R" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
expected = str(root / "build/initramfs.list")
required = ('FILE_LOCKING', 'NET', 'NET_9P', 'NET_9P_VIRTIO', '9P_FS',
            'NETWORK_FILESYSTEMS', 'VIRTIO_MENU', 'VIRTIO', 'VIRTIO_MMIO', 'NETFS_SUPPORT')
for name in ('.config', 'include/config/auto.conf'):
    path = root / 'build/linux' / name
    lines = path.read_text().splitlines()
    values = [line.partition("=")[2].strip('"') for line in lines
              if line.startswith("CONFIG_INITRAMFS_SOURCE=")]
    if values != [expected]:
        sys.exit(f'Invalid INITRAMFS_SOURCE in {path}; stopping before compilation')
    missing = [symbol for symbol in required if f'CONFIG_{symbol}=y' not in lines]
    if missing:
        sys.exit(f'Missing built-in features in {path}: {", ".join(missing)}; '
                 'stopping before compilation')
    if any(line.startswith('CONFIG_NET_9P_FD=') for line in lines):
        sys.exit(f'Unexpected 9P FD transport in {path}; stopping before compilation')
PY
