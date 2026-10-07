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
| `docs/kotama-integration-proposal.md` | original Kotama integration proposal; UART transport remains future work |
| `docs/host-elf-loading.md` | implemented ELF upload/readback verification and CPU hold/start; usage and tests |
| `docs/erbium-qemu-TODO.md` | open questions |
| `docs/sfdp-rtl.hex`, `tools/gen_sfdp_from_trm.py` | SFDP image from RTL / generator from TRM |
| `scripts/run-e2e.sh` | start erbium_emu with the mailbox worker and run the qtests end-to-end |
| `scripts/apply-patches.sh` | re-apply both series on fresh clones |
| `linux/` | guest kernel: patches (driver + cadence fix), config fragment, initramfs, `erbctl`, in-guest test — see `linux/README.md` |
| `scripts/run-linux.sh` | boot the guest (`--test` for the autotest, `--backend SOCK` for erbium_emu) |

`ext/` (Xilinx QEMU, linux, core-et-erbium RTL) and `et-platform/` are fetched by `scripts/fetch-sources.sh`
at pinned revisions and patched; they are not tracked here. `.github/workflows/build.yml` builds and tests
everything on every push and attaches a `dist` tarball (release on `v*` tags).

## Quick start

**From source (Ubuntu 24.04):**

```bash
git clone https://github.com/gh-ainekko/qemu-erbium.git && cd qemu-erbium
./bootstrap.sh          # apt deps, fetch pinned Xilinx QEMU / et-platform / linux 6.12 + apply patches,
                        # build everything into dist/, run the end-to-end test (~25-45 min)
```

**From a release tarball** (built by the GitHub Actions workflow, see the *Actions* tab or *Releases* for tags `v*`):

```bash
sudo apt-get update
sudo apt-get install libglib2.0-0t64 libpixman-1-0 libfdt1 libslirp0 libgcrypt20 zlib1g libstdc++6 libgcc-s1
sha256sum -c erbium-emu-dist-VERSION-ubuntu24.04-x86_64.tar.gz.sha256
tar xzf erbium-emu-dist-*.tar.gz && cd dist
scripts/run-e2e.sh          # 15 qtests + Linux guest test against erbium_emu (~30 s)
scripts/run-linux.sh        # interactive guest shell; try: erbctl info, cat /proc/mtd, erbctl job 0x01000040
```

**Host-load an ELF:** `scripts/run-load-test.sh` boots Linux against an empty,
held backend and tests upload/verify/start/reload through xSPI (no emulator ELF
preload). To use your own ELF, rebuild with
`ERBIUM_ELF=/path/image.elf J=2 scripts/build-all.sh linux`, start the backend with
`--start-held`, and run `erbctl load /firmware/host-payload.elf --verify --start`
inside Linux. See `docs/host-elf-loading.md` for the complete sequence and limits.

**Pieces** (all driven by `scripts/`):

```bash
scripts/fetch-sources.sh    # ext/qemu @59fb95c + qemu-patches, et-platform @836a4ab + sysemu-patches, linux + linux/patches
scripts/build-all.sh [qemu|sysemu|firmware|linux]
scripts/package-dist.sh VERSION  # package the complete dist/ tree into out/ + SHA-256 checksum
scripts/run-e2e.sh          # start erbium_emu with mailbox_worker.elf, run qtests (stub + backend), boot Linux --test
scripts/run-linux.sh [--test] [--backend SOCK] [--mram FILE]
```

Packaging requires all built binaries, firmware, the guest kernel, QEMU data directory,
and shipped documentation; missing inputs fail rather than creating a partial release.
The archive uses sorted entries, normalized ownership/permissions and timestamps, and
timestamp-free gzip headers. `SOURCE_DATE_EPOCH` overrides the default timestamp (the
latest checkout commit); identical `dist/` contents produce identical archives.

Running QEMU by hand:

```bash
erbium_emu -minions 0x1 -single_thread -elf mailbox_worker.elf --api-socket /tmp/erb.sock --mram-file /tmp/mram.img &
qemu-system-aarch64 -M xlnx-versal-virt,ospi-flash=erbium-xspi \
  -object memory-backend-file,id=mram,size=16M,mem-path=/tmp/mram.img,share=on \
  -global erbium-xspi.memdev=mram \
  -chardev socket,id=erb,path=/tmp/erb.sock -global erbium-xspi.chardev=erb \
  -global driver=xlnx.versal-ospi,property=faithful-frames,value=on \
  -kernel Image -append 'console=ttyAMA0 erbium.backend' -display none -serial mon:stdio
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

## Pristine-container validation

With Docker installed, run `J=2 scripts/test-pristine-docker.sh`. It tests **HEAD**
(commit local changes first), using only tracked files in a fresh Ubuntu 24.04
container, with no host build tools or caches. It runs `bootstrap.sh`, regression
tests and packaging, then tests the tarball in a second container with only the
listed runtime libraries. Both backend and stub guest tests must pass.
Logs, image digest, tested commit and tarball are saved in `build/container-test/`.
