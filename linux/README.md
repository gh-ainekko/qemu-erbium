# Linux guest for the Erbium emulation platform

Kernel 6.12.48 (`ext/linux`, pristine tarball + `patches/`), booted on QEMU `xlnx-versal-virt`
with the `erbium-xspi` device behind the Versal Cadence OSPI controller.

| path | what |
|---|---|
| `patches/0001-*` | `spi-cadence-quadspi`: PIO fallback when no PM firmware drives the Versal OSPI DMA mux |
| `patches/0002-*` | `drivers/mtd/devices/erbium-xspi.c`, `include/uapi/linux/erbium-xspi.h`, DT binding `ainekko,erbium-xspi` |
| `erbium.config` | Kconfig fragment (allnoconfig + this; ~8 min build on 2 vCPUs) |
| `initramfs.list`, `rootfs/` | built-in initramfs: static busybox (Ubuntu arm64 `busybox-static`), `init`, `erbium-test.sh` |
| `tools/erbctl.c` | userspace tool for `/dev/erbiumN` (built static into `build/erbctl`) |

## Build

Use the **complete source repository**, not a kernel tarball or binary distribution.
The scripts resolve paths from their own location; no hand-written relative patch paths are needed.

```bash
git clone https://github.com/gh-ainekko/qemu-erbium.git
cd qemu-erbium
./bootstrap.sh              # check checkout, install/check deps, fetch, build, test
```

For a Linux-only rebuild **after the complete bootstrap has succeeded**:

```bash
scripts/preflight.sh build linux
scripts/build-all.sh linux
```

`build-all.sh linux` builds only the guest Image and `erbctl`; it does not build
QEMU, the backend, or minion firmware. If `run-linux.sh` reports a missing
`qemu-system-aarch64`, run `J=2 ./bootstrap.sh` to finish the full stack (existing
build products are reused). For just a stub guest with sources/dependencies
already installed, run `J=2 scripts/build-all.sh qemu`, then
`scripts/run-linux.sh --test`. Do not substitute stock Ubuntu QEMU: it lacks
our Erbium device.

The build expands `@R@` in both configuration templates before running Kconfig,
and refreshes generated configuration on every retry. If an older build failed
with `Cannot open '@R@/build/initramfs.list'`, update the checkout and rerun
`scripts/build-all.sh linux`; deleting `build/linux` is not necessary. Avoid
passing the unexpanded `linux/erbium.config` template directly to `make`.

`linux/rootfs/busybox`, patch files, or `linux/tools/erbctl.c` reported missing by
old manual commands usually means the wrong working directory or an incomplete
checkout, not a missing apt package. Do not continue to `make` after an earlier
step fails. `scripts/preflight.sh checkout` checks tracked inputs without
installing anything; `scripts/preflight.sh deps` also checks tools and libraries.

## Run

```bash
scripts/run-linux.sh                       # interactive shell, built-in stub backend
scripts/run-linux.sh --test                # run /etc/erbium-test.sh and power off
scripts/run-linux.sh --test --backend /tmp/erb.sock   # with erbium_emu (see scripts/run-e2e.sh for starting it)
```

Boot to shell takes ~0.5 s; the full in-guest test (64 KiB MTD write/read/verify, unaligned RMW,
mtdblock, SRAM via ioctl, minion mailbox job, 99h reset) takes ~4 s with the stub and ~7 s with
`erbium_emu` behind the socket.

## Driver (`erbium-xspi`)

* spi-mem client, `compatible = "ainekko,erbium-xspi"`; probes SCCR ID0, parses SFDP (BFPT density,
  with the 16 Mbit erratum fix-up / `ainekko,mram-size` override).
* Registers `mtd0` "erbium-mram" (`MTD_RAM`, 16 MiB, `MTD_NO_ERASE`, erase emulated as 0xFF fill)
  and `/dev/erbium0` (misc) with ioctls `GET_INFO`, `REG_READ/WRITE` (SCCR 65h/71h),
  `MEM_READ/WRITE` (any xSPI address: sysregs/mailboxes at 0x40000000, SRAM 0x4000C000),
  `SFDP_READ`, `SET_RATES` (52h), `RESET` (99h); sysfs `rates`, `chip_id`, `mailbox0`, `mailbox1`.
* Switches to 8D-8D-8D at probe when the controller supports it (Versal OSPI does; module param
  `octal_dtr=0` or DT `ainekko,no-octal-dtr` disables). 1S-1S-1S otherwise. Other Erbium rates
  (4S/4D/8S) need an opcode-extension byte in STR which the Cadence controller cannot send.
* Follows the RTL constraints: reads ≤ one AXI burst per frame (8 B, or 128 B with
  `CFG.BurstEnable` which is switched on only around MRAM bulk reads because the burst is issued
  at the NoC regardless of how many bytes the host clocks); writes in 8-byte beats, RMW for
  unaligned head/tail, frames never cross a 4 KiB page; latency 8 + `CFG.InitialLatency` cycles;
  3 address bytes for register/SFDP ops at S1 command rate, 4 otherwise.
* All addressed ops go through the controller's STIG (≤ 8 B reads) or indirect (larger) paths;
  DAC is not used.

## Known gaps / TODO

* The Cadence controller auto-inserts WREN (06h) before indirect writes and, unless
  `CQSPI_NO_SUPPORT_WR_COMPLETION`, polls RDSR (05h) afterwards. The QEMU model ignores 06h and
  never polls; real Erbium would flag 05h as an illegal command. Needs a controller quirk
  (per-flash "no write completion") for real hardware.
* The Versal OSPI DMA read path needs TF-A/PM firmware; patch 0001 falls back to PIO.
* No interrupt line from Erbium to the host; `erbctl job` polls Mailbox1.
