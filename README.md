# erbium-emu — QEMU emulation platform for the Erbium chip

QEMU models the Erbium **xSPI target** (`erbium-xspi`, an SSI peripheral hanging off the Cadence
OSPI controller of `xlnx-versal-virt`); the Erbium **CPU subsystem** runs out of process in
`erbium_emu` (sw-sysemu). The two share the 16 MiB MRAM through an mmap'd file and talk over a
UNIX socket that models the xSPI IP's AXI port into the NoC (`docs/protocol.md`).

```
 host guest (Linux/qtest) -> Versal OSPI -> erbium-xspi ==(mmap)== erbium-mram.img ==(mmap)== erbium_emu (8x RISC-V minions,
                                              |  SCCR, SFDP                                   |  sysregs, mailbox, PLIC, UART…)
                                              +---------------- UNIX socket (READ/WRITE/RESET) -+
```

## Layout

| path | what |
|---|---|
| `qemu-patches/` | patch series on top of Xilinx QEMU (`ext/qemu`, branch `erbium`): device, OSPI faithful-frames, qtests |
| `sysemu-patches/` | patch series on top of et-platform (`et-platform`, branch `erbium-qemu-backend`): `MmapRegion`, `SocketAgent`, SCCR stub, worker firmware |
| `docs/erbium-qemu-review.md` | review of the original brief and the (revised) plan |
| `docs/rtl-xspi-findings.md` | RTL ground truth for the xSPI IP (contradicts the TRM in places) — the model follows this |
| `docs/qemu-ospi-notes.md` | how the Versal OSPI model frames transactions; build notes |
| `docs/sysemu-notes.md` | building/running `erbium_emu`, test ELFs, socket backend |
| `docs/protocol.md` | QEMU <-> erbium_emu socket protocol v1 |
| `docs/erbium-qemu-TODO.md` | open questions |
| `docs/sfdp-rtl.hex`, `tools/gen_sfdp_from_trm.py` | SFDP image from RTL / generator from TRM |
| `scripts/run-e2e.sh` | start erbium_emu with the mailbox worker and run the qtests end-to-end |
| `scripts/apply-patches.sh` | re-apply both series on fresh clones |
| `linux/` | guest kernel: patches (driver + cadence fix), config fragment, initramfs, `erbctl`, in-guest test — see `linux/README.md` |
| `scripts/run-linux.sh` | boot the guest (`--test` for the autotest, `--backend SOCK` for erbium_emu) |

`ext/` (Xilinx QEMU, core-et-erbium RTL) and `et-platform/` are separate clones, not tracked here.

## Quick start

```bash
# QEMU (see docs/qemu-ospi-notes.md §0 for the configure line)
ninja -C ext/qemu/build qemu-system-aarch64 tests/qtest/erbium-xspi-test
(cd ext/qemu/build && QTEST_QEMU_BINARY=./qemu-system-aarch64 tests/qtest/erbium-xspi-test)   # stub backend

# erbium_emu + worker firmware (docs/sysemu-notes.md §1-2), then:
./scripts/run-e2e.sh                                                                          # real CPU backend

# Running QEMU by hand
erbium_emu -minions 0x1 -single_thread -elf mailbox_worker.elf --api-socket /tmp/erb.sock --mram-file /tmp/mram.img &
qemu-system-aarch64 -M xlnx-versal-virt,ospi-flash=erbium-xspi \
  -object memory-backend-file,id=mram,size=16M,mem-path=/tmp/mram.img,share=on \
  -global erbium-xspi.memdev=mram \
  -chardev socket,id=erb,path=/tmp/erb.sock -global erbium-xspi.chardev=erb \
  -global driver=xlnx.versal-ospi,property=faithful-frames,value=on ...
```

## Status

* M1 `erbium-xspi` device: done — 1S and logical 4S/4D/8S/8D, SFDP (from TRM, matches RTL), SCCR,
  memory ops over the full xSPI map, RTL burst/latency/wrap semantics, 99h chip reset; 7 qtests.
* M3 backend: done — `erbium_emu` serves the socket, shares MRAM, `mailbox_worker.elf` completes
  host-submitted CRC/fill jobs end-to-end through xSPI (qtest `backend-mailbox`).
* M2 Linux driver: done — `drivers/mtd/devices/erbium-xspi.c` (spi-mem client; MTD RAM +
  `/dev/erbiumN` control plane, 8D-8D-8D), QEMU generates the OSPI/flash DT nodes, guest kernel
  6.12 + busybox initramfs boots in 0.5 s and `scripts/run-linux.sh --test [--backend SOCK]` runs
  the in-guest end-to-end test including a minion mailbox job. See `linux/README.md`.
* HyperBus profile: not implemented (logged as unimplemented).
