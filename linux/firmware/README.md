# MRAM-only host-loader fixture

Build with `scripts/build-load-test.sh` using the stock
`riscv64-unknown-elf-{gcc,ld,readelf}` tools (override `RISCV_CROSS_COMPILE` if needed).
The output is `build/loader-smoke.elf`. No libc, stack, platform includes,
custom instructions/CSRs, UART, or other firmware is needed.

The ELF is ELF64 little-endian RISC-V ET_EXEC, with exactly two PT_LOADs:

| segment | CPU address / p_paddr | file size | memory size |
|---|---:|---:|---:|
| RX text | `0x40010000` | `0x1407` | `0x1407` |
| RW data + gap + BSS | `0x40013000` | `0x1007` | `0x3020` |

Both file-backed segments cross 4 KiB boundaries and end on partial
eight-byte beats. **e_entry is `0x40010040`, not either segment base**.
Starting at the text base intentionally produces failure code `bad00004`.
The fixture checks initialized data, text/data boundary-tail words and final
bytes, and every BSS byte. It then dirties every BSS byte with `0xa5` and
publishes success. Success and failure both enter persistent loops.

## Addresses and markers

`erbctl mem32` takes **xSPI addresses**, not CPU MRAM addresses.

| item | CPU address | xSPI address / MTD offset | expected word |
|---|---:|---:|---|
| text boundary tail | `0x40011400` | `0x00011400` | `5eedc0de` |
| initialized data | `0x40013000` | `0x00013000` | `13579bdf` |
| data boundary tail | `0x40014000` | `0x00014000` | `2468ace0` |
| zero-tail aligned probe | `0x40014008` | `0x00014008` | `00000000` |
| BSS start | `0x40015000` | `0x00015000` | zero before start; `a5a5a5a5` after |
| BSS last eight bytes | `0x40016018` | `0x00016018` | zero before start; `a5a5a5a5` after |
| MAILBOX0, signature | `0x02000068` | `0x40000068` | `4c44534d` on success |
| MAILBOX1, result | `0x02000070` | `0x40000070` | `600d600d` on success |

BSS is `[0x40015000, 0x40016020)`. Failure signature is `4641494c`;
results are `bad00001` (data), `bad00002` (BSS), `bad00003` (text),
`bad00004` (wrong entry). MAILBOX1 is published last.

## Integration

`scripts/build-all.sh linux` builds this fixture before the initramfs. The global
build/init/runner scripts implement the following integration:

1. Build the fixture before building the Linux initramfs.
2. Add `dir /firmware 0755 0 0` and these entries to `linux/initramfs.list`:
   ```
   file /firmware/loader-smoke.elf @R@/build/loader-smoke.elf 0644 0 0
   file /etc/erbium-load-test.sh @R@/linux/rootfs/erbium-load-test.sh 0755 0 0
   ```
3. `/init` should run `/etc/erbium-load-test.sh` for `erbium.loadtest`, print
   `ERBIUM-LOAD-TEST-RESULT <rc>`, and power off for `erbium.poweroff`.
4. Run against the real socket backend initially held (`--start-held`), with
   kernel flags `erbium.backend erbium.loadtest erbium.poweroff`. Do not run
   the mailbox-worker autotest in the same session: this replaces firmware.
5. Package the fixture into `dist/firmware` if release runs should support it.

The test uses the agreed `erbctl load FILE [--mtd DEV] [--verify] [--start]
[--check]` interface only, plus existing `mem32`, BusyBox shell, `cp`, `dd`,
`grep` and fractional `sleep`. It verifies default held loading, explicit
verify/start, repeated BSS clearing, non-destructive valid/invalid `--check`,
invalid/missing ELF with `--start`, and MTD-open failure without a start.
It reads SoftReset (`0x40000028`, held `6`, released `4`),
thread-0/thread-1 disable masks (`0x80f40240`/`0x80f40010`, all disabled
`ff`, only minion 0/thread 0 enabled `fe`/`ff`), and the boot PC
(`0x80d00018`) to catch a warm reset that preserves mailbox contents.
Malformed magic and truncated-header copies are generated in `/tmp`, so only
one ELF needs packaging. `LOAD_TEST_ELF` and `LOAD_TEST_MTD` may override the
fixture and MTD paths.
