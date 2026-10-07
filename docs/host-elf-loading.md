# Host ELF loading and CPU hold/start

Implemented: CPU warm-reset hold/release in sysemu, a strict userspace ELF
loader in `erbctl`, and a Linux → xSPI → MRAM → RISC-V execution test.
No new UART transport or Kotama console bridge is included.

## Update and test

From the full source checkout:

```sh
git pull
J=2 ./bootstrap.sh
```

The fetch step now appends missing patches to existing, clean source clones
when their commits match the pinned patch-series prefix. It compares patch IDs,
not commit hashes. It refuses dirty, custom, reordered or changed histories;
it never resets/deletes your work. In particular, an older four-patch sysemu
checkout gets the reset/start patches before building.

The ordinary end-to-end suite now includes the loader test. To run it alone:

```sh
scripts/run-load-test.sh
```

This creates **empty MRAM**, starts `erbium_emu --start-held` with **no `-elf`**, boots
Linux, and has Linux load the test ELF through `/dev/mtd0` and `/dev/erbium0`.
The RISC-V program checks initialized data and BSS and returns a mailbox marker.
The test repeats the load after the program has dirtied BSS, checks held vs.
released state, and rejects malformed images without resetting a running hart.
Success requires `ERBIUM-LOAD-TEST-RESULT 0`, not merely a successful QEMU exit.
The fixture and test script are embedded in the Image, so the test also works
from the binary release using `dist/scripts/run-load-test.sh`.

## Load your own ELF

Build an Image containing the supplied file in the Linux initramfs:

```sh
ERBIUM_ELF=/absolute/path/kotama.elf J=2 scripts/build-all.sh linux
```

The build validates the supplied ELF with a native `erbctl --check` first.
This only copies the ELF into **host Linux's filesystem** at
`/firmware/host-payload.elf`. It does **not** preload Erbium MRAM or change its
reset PC. Rebuilding without `ERBIUM_ELF` removes the optional file from the next
Image. The normal `/firmware/loader-smoke.elf` remains available either way.

Start the backend in one terminal (use `dist/bin/erbium_emu` for a release):

```sh
build/sw-sysemu/erbium_emu -single_thread -minions 0x1 --start-held \
  --api-socket /tmp/erb-load.sock --mram-file /tmp/erb-load.img
```

Boot Linux in another:

```sh
scripts/run-linux.sh --backend /tmp/erb-load.sock --mram /tmp/erb-load.img
```

Inside **Linux**, not on the development machine:

```sh
# Validate without accessing devices or affecting a running CPU:
erbctl load /firmware/host-payload.elf --check

# Upload and read back every segment/zero-fill byte; leave all threads held:
erbctl load /firmware/host-payload.elf --verify

# Reload, verify and release minion 0/thread 0 at the ELF entry point:
erbctl load /firmware/host-payload.elf --verify --start

# Stop the CPUs without erasing MRAM:
erbctl hold
```

`--verify` is accepted for clarity but verification is **always on**. There is
no unchecked-start mode. The default is to remain held. `--check` and `--start`
are mutually exclusive. Successful `--start` means the image was verified and
the reset was released, **not** that firmware readiness was observed. The smoke
test separately requires the firmware's mailbox response. UART readiness for
arbitrary Kotama images remains a later integration step.

For multiple devices, specify both `erbctl -d /dev/erbiumN load ... --mtd
/dev/mtdM`. The tool checks that the MTD is RAM, has the expected size, and has
the **same sysfs parent** as the control device. `/sys` must be mounted; it
refuses regular files, block devices, or an unrelated MTD.

Do not use `erbctl reset`/99h as a start command. That is whole-chip POR and
reapplies backend startup defaults, including `--start-held` if selected.

## Supported ELF subset

- ELF64 little-endian RISC-V `ET_EXEC`.
- At most 128 program headers, file size at most 64 MiB.
- Matching physical/virtual addresses, wholly inside CPU MRAM
  `[0x40000000, 0x41000000)` and the actual detected device capacity.
- Nonoverlapping `PT_LOAD` memory ranges; complete, overflow-checked file ranges,
  `p_filesz <= p_memsz`, and valid segment alignment.
- Even entry address inside file-backed executable load data.
- No dynamic/interpreter/TLS segments, relocation/dynamic sections or extended
  ELF header numbering. Section headers are optional for stripped images.

All ELF validation happens against an in-memory snapshot **before device
access**. Image bytes are written to MTD offset `p_paddr - 0x40000000`, not to
an address guessed from the file name or ELF text link base. BSS/zero-fill is
written explicitly on every load. Register writes use full 8-byte transfers
with little-endian values, while the driver handles MRAM partial beats and
4-KiB frame boundaries.

A validation/device-open failure leaves existing execution untouched. After
CPU hold is attempted, an upload, verification, register, or handled
SIGINT/SIGTERM failure tries to reassert hold and disable all threads. Failure
to confirm that state is explicitly reported (e.g. disconnected backend).
Cancellation is checked again after release readback before committing success.
A cancellation caught during release attempts to re-hold the CPU; it cannot
retroactively prevent instructions already executed during that transition.
Signals received after successful completion do not undo the completed boot.
SIGKILL/power loss cannot be caught, but the CPU is held before the first MRAM
write and is released only after complete verification.

`erbctl` uses advisory locks on the control device; the loader additionally
locks its MTD fd. This serializes cooperating `erbctl` clients, **not** raw
MTD/ioctl users. Stop other clients before loading. The guest kernel now enables
`CONFIG_FILE_LOCKING`, including when updating an existing minimal config.

## Reset model

The xSPI System `SoftReset` address is `0x40000028` (CPU address `0x02000028`):

| Bit | Function |
|---|---|
| 0 | Whole-system reset; reapplies the startup/POR profile |
| 1 | Persistent CPU warm-reset hold |
| 2 | Active-low MRAM reset (`mram_rst_b`); keep it **1** for normal operation |
| 3 | Self-clearing CPU cold-reset pulse, distinct from whole-system POR |

The loader writes `0x6` to hold and `0x4` to release; it never writes `0x2`.
While held, changing thread masks cannot start execution, the host socket
remains responsive, and the host may program the ESR boot PC. Release resamples
`MINION_BOOT` (`0x80d00018`) and hart reset/fetch state, retaining host-programmed
ESRs and memory. It enables only thread 0 on minion 0 via masks `0xfe` and `0xff`.
Cold pulse resets CPU/ESRs without erasing memory or reapplying CLI defaults.
Socket RESET kind 1 remains xSPI-only, with no CPU side effect.

`--start-held` is an explicit startup/POR profile analogous to the documented
TestMode CPU hold; it is not a new socket boot command. Default launches without
that option retain their existing running behavior. The model remains functional,
not cycle-accurate PRCM timing; MRAM reset-pin controller timing and complete
peripheral POR modeling remain outside this change. UART behavior is unchanged.

## Verification performed (2026-10-07)

- 154 offline native ELF parser, mocked device/fault and CLI tests, including
  zero hardware access on malformed images, short I/O, mismatched device pairing,
  corrupt readback, register failures, cancellation and hold cleanup.
- 28 offline source-update tests (fresh/prefix/current/dirty/custom/changed).
- Backend socket tests: held startup, masks, changed boot PC, preserved ESRs,
  repeat uploads, CSR/fetch reset, WFI/socket liveness and reconnection, CPU-origin
  reset writes, warm/cold/whole-system/xSPI-only resets, default startup,
  pipelined reset/write ordering, debug-release exclusion and four-hart
  firmware-triggered debug-reset recovery without losing socket service.
- Existing 15 qtests and Linux mailbox-worker suite pass.
- New Linux-host loader suite passes through the full xSPI path, from empty MRAM,
  including repeated execution and BSS clearing, without emulator ELF preload.
- **Actual Kotama** from the proposal's pinned baseline was embedded only in
  Linux's initramfs, uploaded/verified by guest `erbctl`, and started from empty
  MRAM. Its `tamago/riscv64 (go1.27.1)` banner and shell prompt appeared through
  the existing backend `-uart_tx_file` diagnostic output. No UART bridge was
  added and no interactive host-console claim is made. Kotama/toolchain building
  is not yet part of the default pipeline.

Useful standalone commands:

```sh
python3 -m unittest discover -s tests -p 'test_erbctl_loader.py'
python3 -m unittest discover -s tests -p 'test_fetch_sources.py'
python3 et-platform/sw-sysemu/tests/erbium/host/test_cpu_reset.py \
  --emu build/sw-sysemu/erbium_emu
scripts/run-e2e.sh
```

### Pristine-container result

`J=2 scripts/test-pristine-docker.sh` passed from committed snapshot
`249f5b35e4a0a980b59676e1400879631048b0bf`, including the full build and a separate
binary-only Ubuntu 24.04 container. Both executed the host-load/reload fixture
through Linux/QEMU/xSPI with empty initial MRAM; all test markers were zero.
See `container-validation.md` for details. The suite also found/fixed an older
qtest startup race: it now waits for the mailbox worker's exact ready marker
rather than assuming firmware has run immediately after chip reset.
