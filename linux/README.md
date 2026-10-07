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

## Host ELF loading

`erbctl load ELF [--verify] [--start | --check] [--mtd /dev/mtd0]` validates a
static MRAM-only RISC-V ELF, uploads it, zeroes BSS and verifies readback. Default:
leave CPUs held; `--start` releases minion 0 at `e_entry`; `--check` only validates.
`erbctl hold` asserts CPU warm reset while preserving MRAM. Source updates include
required backend reset semantics; do not use an older backend that merely stores
the SoftReset field. See `../docs/host-elf-loading.md` for device pairing, limits,
advisory-lock requirements and the full launch sequence.

`scripts/run-load-test.sh` runs the embedded two-segment smoke ELF from empty
MRAM with no emulator preload. To place a user-supplied ELF in the guest, build
with `ERBIUM_ELF=/absolute/path/image.elf J=2 scripts/build-all.sh linux`; it appears
at `/firmware/host-payload.elf` and is **not** automatically loaded into Erbium.
The Linux build now also needs the stock RISC-V cross compiler to build the small
loader fixture. Direct UART wiring and console use are documented in
`docs/uart-console.md`.

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

## Direct Erbium UART

`scripts/run-linux.sh --backend CONTROL --mram MRAM --uart-socket UART` connects
Linux UART1 (`/dev/ttyAMA1`) to the independent Erbium serial-peer socket. UART0
remains the host console. `scripts/run-uart.sh` manages a fresh complete session.

Inside Linux: `erbctl console /dev/ttyAMA1 --load /firmware/host-payload.elf`
attaches raw UART first, then runs the verified ELF loader while relaying output.
Without `--load`, it only opens the tty. Ctrl-] exits an interactive session.
`--uart-test` and `--kotama-test` runner modes automate binary IRQ/WFI echo/reload
and real Kotama `info` acceptance respectively, using the actual guest UART1.

## Host shared folders

Source checkouts: build/update both components once (existing configurations
are upgraded). New binary distributions already contain this support:

```sh
J=2 scripts/build-all.sh qemu
J=2 scripts/build-all.sh linux
```

Export one existing directory to the Linux guest, automatically mounted at
`/mnt/host`:

```sh
scripts/run-linux.sh --share /absolute/host/folder   # read-only default
scripts/run-uart.sh --share /absolute/host/firmware  # UART + xSPI + shared files
scripts/run-uart.sh --share-rw /absolute/host/folder # explicitly allow writes
```

Paths containing spaces work; quote them. Relative paths are resolved before
launch. Commas/newlines and duplicate share options are rejected. Without either
option no host directory is exported. A failed requested mount powers off rather
than leaving an apparently shared, writable directory in guest RAM.

For example, build `application.elf` in the exported host directory, then inside
Linux run:

```sh
ls /mnt/host
erbctl console /dev/ttyAMA1 --load /mnt/host/application.elf
```

No initramfs rebuild is needed when firmware changes. Files are transported by
virtio-9p over the board's existing virtio-MMIO devices, not Ethernet. The share
belongs to ARM Linux, not directly to Erbium: the existing xSPI loader still
uploads/verifies the ELF and the UART remains a separate cable.

Read-only is enforced by QEMU as well as by the guest mount. Read-write exports
allow guest modification/deletion of files in the directory. Run QEMU as your
ordinary user and export only a dedicated directory you trust the guest with,
not your home directory or filesystem root. Shares use `mapped-xattr`: guest
ownership/mode/symlink metadata is represented using host extended attributes
(and guest-created symlinks use QEMU's mapped representation). RW exports require
a filesystem supporting user xattrs; host permissions need not equal the guest's
virtual permissions. File contents remain ordinary host files. Mounts use
`nodev,nosuid,cache=none`; host changes are visible, but concurrent writes still
require coordination. Rebuild ELF outputs completely before starting a load.

`scripts/run-share-test.sh` exercises RO enforcement even after a guest-root
remount attempt, RW creation/appending visible on the host, paths with spaces,
and host-side updates visible to an already-mounted guest. It is included in
E2E and binary-distribution tests.

Shared-folder sessions explicitly use **single-thread TCG**, still exposing both
ARM guest CPUs. The pinned QEMU/kernel combination showed an intermittent SMP
queued-spinlock crash/hang under concurrent 9P reads with multithreaded TCG.
Serialized TCG is a tested mitigation, not a claimed upstream root-cause fix;
it can reduce parallel guest CPU throughput. Non-shared sessions retain their
existing accelerator defaults. The Erbium backend remains a separate process.
