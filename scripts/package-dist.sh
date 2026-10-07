#!/usr/bin/env bash
# Package an already built dist/ tree, identically locally and in Actions.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[[:alnum:]][[:alnum:]._-]*$ ]]; then
  echo "usage: $0 VERSION (letters, numbers, dots, underscores and hyphens)" >&2
  exit 2
fi
V=$1
cd "$R"

# Validate everything before copying documentation or creating an archive.
for f in dist/bin/qemu-system-aarch64 dist/bin/erbium_emu \
         dist/tests/erbium-xspi-test dist/bin/erbctl-aarch64 \
         dist/firmware/mailbox_worker.elf dist/firmware/loader-smoke.elf dist/firmware/uart-smoke.elf dist/linux/Image \
         scripts/env.sh scripts/run-linux.sh scripts/run-e2e.sh scripts/run-load-test.sh scripts/run-uart.sh scripts/run-uart-test.sh scripts/run-share-test.sh \
         README.md linux/README.md docs/protocol.md docs/host-elf-loading.md docs/uart-console.md docs/erbium-qemu-TODO.md; do
  [ -s "$f" ] || { echo "Required package file missing or empty: $f" >&2; exit 1; }
done
for f in dist/bin/qemu-system-aarch64 dist/bin/erbium_emu \
         dist/tests/erbium-xspi-test scripts/run-linux.sh scripts/run-e2e.sh scripts/run-load-test.sh scripts/run-uart.sh scripts/run-uart-test.sh scripts/run-share-test.sh; do
  [ -x "$f" ] || { echo "Required package file not executable: $f" >&2; exit 1; }
done
[ -d dist/share/qemu ] || { echo "Required package directory missing: dist/share/qemu" >&2; exit 1; }

# Normalize archive metadata and gzip headers; reproducible for identical dist contents.
EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}
[[ "$EPOCH" =~ ^[0-9]+$ ]] || { echo "SOURCE_DATE_EPOCH must be a nonnegative integer" >&2; exit 2; }
mkdir -p out dist/scripts dist/docs
cp scripts/env.sh scripts/run-linux.sh scripts/run-e2e.sh scripts/run-load-test.sh scripts/run-uart.sh scripts/run-uart-test.sh scripts/run-share-test.sh dist/scripts/
cp README.md dist/docs/README.md
cp linux/README.md dist/linux/README.md
cp docs/protocol.md docs/host-elf-loading.md docs/uart-console.md docs/erbium-qemu-TODO.md dist/docs/
cat > dist/README.md <<'MD'
# erbium-emu binary distribution

Built for Ubuntu 24.04 (x86_64). Install host runtime dependencies:

```bash
sudo apt-get update
sudo apt-get install libglib2.0-0t64 libpixman-1-0 libfdt1 libslirp0 libgcrypt20 libattr1 zlib1g libstdc++6 libgcc-s1
scripts/run-e2e.sh          # qtests + mailbox, ELF loader and UART guest tests
scripts/run-load-test.sh     # empty-MRAM host upload/verify/start/reload test only
scripts/run-share-test.sh    # read-only/read-write host shared-folder tests
scripts/run-uart-test.sh     # Linux UART1 ↔ Erbium UART binary echo/reload test
scripts/run-uart.sh          # fresh held Erbium + interactive Linux with serial cable
scripts/run-linux.sh --share /absolute/host/folder  # read-only at /mnt/host
scripts/run-uart.sh --share-rw /absolute/host/folder # explicit guest write access
scripts/run-linux.sh        # interactive guest shell (built-in stub backend)
scripts/run-linux.sh --backend /tmp/erb.sock   # after starting bin/erbium_emu --api-socket /tmp/erb.sock --mram-file /tmp/mram.img
```

See docs/ for the design, protocol and open questions, and linux/README.md for the guest.
MD
ARCHIVE="erbium-emu-dist-$V-ubuntu24.04-x86_64.tar.gz"
tar --sort=name --mtime="@$EPOCH" --owner=0 --group=0 --numeric-owner \
    --mode='u+rwX,go+rX,go-w' -cf - dist | gzip -n > "out/$ARCHIVE"
(cd out && sha256sum "$ARCHIVE" > "$ARCHIVE.sha256")
echo "Created out/$ARCHIVE and out/$ARCHIVE.sha256"
