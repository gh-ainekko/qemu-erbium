#!/usr/bin/env bash
# Export the kernel changes (against the pristine 6.12.48 tarball) into linux/patches/.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
L=$R/ext/linux
P=$R/linux/patches
PR=${PRISTINE:-/tmp/pristine/a}
mkdir -p "$P"
MOD="drivers/spi/spi-cadence-quadspi.c drivers/mtd/devices/Kconfig drivers/mtd/devices/Makefile"
NEW="drivers/mtd/devices/erbium-xspi.c include/uapi/linux/erbium-xspi.h Documentation/devicetree/bindings/mtd/ainekko,erbium-xspi.yaml"
for f in $MOD; do
  if [ ! -f "$PR/$f" ]; then
    mkdir -p "$PR/$(dirname $f)"
    curl -sSL "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/$f?h=v6.12.48" -o "$PR/$f"
  fi
done
{
  echo "From: Erbium emulation <roman@nekko.ai>"
  echo "Subject: [PATCH 1/2] spi: cadence-quadspi: fall back to PIO when no PM firmware drives the Versal OSPI DMA mux"
  echo
  echo "zynqmp_pm_ospi_mux_select() returns -ENODEV when CONFIG_ZYNQMP_FIRMWARE"
  echo "is off or no TF-A is present (e.g. QEMU xlnx-versal-virt). Instead of"
  echo "failing every read larger than 3 bytes, disable the DMA path and use"
  echo "indirect PIO reads."
  echo "---"
  (cd / && diff -u "$PR/drivers/spi/spi-cadence-quadspi.c" "$L/drivers/spi/spi-cadence-quadspi.c" \
     | sed -e "1s|^--- .*|--- a/drivers/spi/spi-cadence-quadspi.c|" -e "2s|^+++ .*|+++ b/drivers/spi/spi-cadence-quadspi.c|") || true
} > "$P/0001-spi-cadence-quadspi-pio-fallback-without-pm-firmware.patch"
{
  echo "From: Erbium emulation <roman@nekko.ai>"
  echo "Subject: [PATCH 2/2] mtd: devices: add Erbium xSPI target driver (MRAM MTD + /dev/erbiumN control plane)"
  echo
  echo "spi-mem client for the Erbium AI chip's xSPI Profile 2.0 port."
  echo "---"
  for f in $MOD; do
    [ "$f" = drivers/spi/spi-cadence-quadspi.c ] && continue
    (cd / && diff -u "$PR/$f" "$L/$f" | sed -e "1s|^--- .*|--- a/$f|" -e "2s|^+++ .*|+++ b/$f|") || true
  done
  for f in $NEW; do
    (cd / && diff -u /dev/null "$L/$f" | sed -e "1s|^--- .*|--- /dev/null|" -e "2s|^+++ .*|+++ b/$f|") || true
  done
} > "$P/0002-mtd-devices-add-erbium-xspi-driver.patch"
ls -la "$P"
