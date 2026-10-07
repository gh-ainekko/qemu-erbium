# SmallVM checkpoint validation

Date: 2026-10-07. Scope: assessment steps #1 and #2, standalone emulator only.

Command: `scripts/test-smallvm.sh --with-legacy32` — **PASS**.

| Checkpoint | Result |
| --- | --- |
| LP64 host | 772 common assertions; native callback above 4 GiB; invalid handles rejected |
| LP64 ASan + UBSan | Same common suite and callback; fatal sanitizer mode, no findings |
| Legacy 32-bit pointer OBJ | 772 common assertions under qemu-i386 + UBSan |
| RV64 firmware | 772 common assertions; clean single-hart operation |
| Poisoned MRAM / eight minions | Data/BSS/TLS checks and common suite pass; one VM initializer |
| BSS negative control | Removing clear store causes the expected startup rejection |
| Timer | 600 MTIME ticks / 60,010 independent cycles; 1 ms delay ~199,912 cycles |
| Timer negative control | Wrong 10 MHz conversion rejected |
| Fault injection | Illegal instruction reports cause 2, exact injected PC, mtval 0, MRAM SP |
| Normal UART | Binary ping/version; `v416 Erbium`; no unframed bytes |
| ELF/ISA | Static ELF64 LE, soft-float ABI, no A/F/D/vector, bounded RX/RW MRAM loads |
| Source preparation | Fresh upstream replay has matching tree; four no-overwrite/idempotence tests pass |

The 772 count **includes** the 385 targeted GC/literal/name regressions; it is not 772 plus 385.

## Provenance

- SmallVM base: `49f337529294640def7b27a2ee1c4e7ba88ccdbb`.
- Patched source tree: `38c89014e0c5a9e43cf50e2207b7d0dd00722d1c`.
- Development checkout commit: `b3e0be90e6a024474c3df9c9264d486be0408b10` (git-am may produce different commit IDs with the same tree).
- Cross compiler: `riscv64-unknown-elf-gcc (13.2.0-11ubuntu1+12) 13.2.0`.
- C library: picolibc 1.8.6-2 RV64IM/LP64, package checksum pinned in `scripts/build-smallvm.sh`.

Artifact SHA-256:

- `smallvm.elf`: `79aaa5372d0880606ee6b339d42503fbb944b52b1ae154f687fd413ebd37e7d3`.
- `smallvm-selftest.elf`: `f6c0e62460df5c0b5dceb980ca3996c173e51769e7f2b54880c40a772677e3cc`.
- Tested emulator binary: `4f824a5ada637da6095fd402c7414be66b93921205c04bf0a5bf863d8a469548`.

Raw transcripts, exact commands, ELF segment metadata, negative-control ELF mutations, and hashes are in `build/smallvm/results/` and the review archive.

This validation does not cover a full Arduino board build, full IDE project interoperability, arbitrary running-code replacement, physical UART baud, durable persistence, or host/xSPI boot. See `README.md` for limitations and reproduction instructions.
