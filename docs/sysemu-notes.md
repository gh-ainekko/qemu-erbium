# sw-sysemu (`erbium_emu`) build, run and QEMU-backend notes

Work lives on branch `erbium-qemu-backend` of `/home/exedev/erbium-emu/et-platform`
(subdir `sw-sysemu`). Commits:

| commit | what |
|---|---|
| `1a63a76` | fix `-Werror=unused-result` build break in `devices/shakti_uart.h` (GCC 13 / glibc 2.39) |
| `aca1e61` | `memory/mmap_region.h` (`MmapRegion`), `devices/sccr_er.h` (SCCR stub), `MainMemory::{mram_file,access_sizes,por_reset}` |
| `8300603` | `sw-sysemu/socket_agent.{h,cpp}` (`SocketAgent`), `--api-socket`, `--mram-file`, keep-alive/idle poll, POR, signals |
| `4afc2c9` | `tools/erb_client.py`, `tests/erbium/host/mailbox_worker.c` |

## 1. Build

Dependencies (Ubuntu 24.04): `libgoogle-glog-dev liblz4-dev lz4 cmake ninja-build` plus the
header-only `erbium_hal` package (from `et-platform/erbium-hal`) and, for test ELFs,
`gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf`.

```bash
sudo apt-get install -y libgoogle-glog-dev liblz4-dev lz4 cmake ninja-build \
                        gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf

R=/home/exedev/erbium-emu
# erbium_hal (INTERFACE lib) -> local prefix
cmake -S $R/et-platform/erbium-hal -B $R/build/erbium-hal -DCMAKE_INSTALL_PREFIX=$R/build/prefix
cmake --install $R/build/erbium-hal

# sw-sysemu, Release -O2, only the erbium_emu target
cmake -S $R/et-platform/sw-sysemu -B $R/build/sw-sysemu -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS_RELEASE=-O2 -DCMAKE_C_FLAGS_RELEASE=-O2 \
      -DCMAKE_PREFIX_PATH=$R/build/prefix
ninja -C $R/build/sw-sysemu -j2 erbium_emu         # ~10 min at -j1 on the 2-vCPU VM
```

Result: `$R/build/sw-sysemu/erbium_emu` (~1.2 MB). `cmake/Findglog.cmake`/`Findlz4.cmake` find the
Ubuntu packages without extra hints. Only one trivial build break had to be fixed (commit `1a63a76`).

## 2. Building test ELFs with the stock Ubuntu cross compiler

`tests/erbium/Makefile` expects the Esperanto toolchain in `/opt/et/bin`, which knows the custom CSRs
`validation0`/`tensor_mask` and the `mova.m.x` instruction. The stock `riscv64-unknown-elf-gcc` 13.2
works with two tricks (no source changes):

* CSR names -> numbers via assembler `--defsym` (also works for inline asm in C):
  `-Wa,--defsym,validation0=0x8d0 -Wa,--defsym,tensor_mask=0x805`
* `mova.m.x zero` in `common/boot.S` -> `.word 0xD600107B` (custom-3 opcode 0x7b, funct7 0x6b, funct3 1)
  in a patched copy of `boot.S` placed in `build/`.

```bash
cd $R/et-platform/sw-sysemu/tests/erbium
mkdir -p build
sed 's/mova\.m\.x zero/.word 0xD600107B  \/* mova.m.x zero *\//' common/boot.S > build/boot.S
make RISCV=/usr/bin BOOT_SRCS="build/boot.S common/crt.S common/trap.S" \
     CPPFLAGS="-Iinclude -Icommon -Wa,--defsym,validation0=0x8d0 -Wa,--defsym,tensor_mask=0x805" \
     build/dummy_pass.elf build/pma_mram_rw.elf

# run one (memory map: bootrom 0x02008000, SRAM 0x0200C000, MRAM 0x40000000, sysregs 0x02000000)
$R/build/sw-sysemu/erbium_emu -minions 0xff -mins_dis -elf build/dummy_pass.elf
#   ... 98: INFO EMU: [H0 S0:N0:C0:T0] Signal end test with PASS
```

The `-Wa,--defsym` trick is not wired into the Makefile (the Makefile is meant for the ET toolchain);
the host-driven firmware below is compiled by hand the same way:

```bash
riscv64-unknown-elf-gcc -Iinclude -Icommon -Wa,--defsym,validation0=0x8d0 -Wa,--defsym,tensor_mask=0x805 \
  -Wall -Wextra -Werror -nostdlib -O2 -g -mcmodel=medany -march=rv64imfc -mabi=lp64f \
  -Tcommon/erbium.ld -Wl,--section-start=bootrom=0x200a000 -Wl,--no-warn-rwx-segments \
  -o build/mailbox_worker.elf host/mailbox_worker.c build/boot.S common/crt.S common/trap.S
```

`examples/bin/test.elf` is for the ETSOC-1 map (0x8000001000), not usable with `erbium_emu`.

## 3. Running the QEMU backend

```bash
# no firmware: only serves the socket (hart 0 boots from the empty ROM, faults, and is taken offline)
$R/build/sw-sysemu/erbium_emu -minions 0xff --api-socket /tmp/erb.sock --mram-file /tmp/mram.img

# with the mailbox worker firmware on minion 0
$R/build/sw-sysemu/erbium_emu -minions 0x1 -single_thread -elf tests/erbium/build/mailbox_worker.elf \
     --api-socket /tmp/erb.sock --mram-file /tmp/mram.img
```

* `--mram-file` creates/grows the file to 16 MiB, never truncates it; contents survive `RESET` and restarts.
  Without it MRAM is the usual in-process `DenseRegion` (and `-mem_reset` patterns apply as before).
* `--api-socket` implies: keep running with no active harts, no default `-max_cycles` limit (pass one
  explicitly if wanted), no "need an ELF" check. Idle (no active harts, no timer running) costs ~0% CPU
  (1 ms `poll()` per loop iteration); a polling firmware pins a core as usual.
* `SIGINT`/`SIGTERM` end the emulation cleanly and unlink the socket. `SIGPIPE` is ignored.
* Underscore spellings `-api_socket`/`-mram_file` are accepted too (`getopt_long_only`, single or double dash).

## 4. Test client

```bash
C="python3 tools/erb_client.py /tmp/erb.sock"
$C ping
$C write 0x02000068 efbeadde -- read 0x02000068 4      # Mailbox0 round trip
$C read 0x02000069 2                                   # -> SLVERR (misaligned register access)
$C read 0x03000000 4                                   # -> DECERR (unmapped)
$C read 0x0200F000 0x38                                # SCCR stub: ID0=2, CFG=0x1382
$C write 0x40000000 48656c6c6f -- read 0x40000ff0 4096 # MRAM through the socket (and hexdump /tmp/mram.img)
$C reset 1 -- reset 0
```

All of the above were verified, plus: partial (byte-by-byte) and pipelined requests, BADREQ on bad
magic/len (>4096), client disconnect + reconnect, 4096-byte MRAM read crossing a page.

End-to-end with `mailbox_worker.elf` (host job word in Mailbox0 `0x02000068`, result in Mailbox1
`0x02000070`): `write 0x02000068 10005a02` fills MRAM+0x1000 with 0x5a (visible in the file),
`write 0x02000068 10000001` returns CRC32 `7cd551dd` in Mailbox1; data written by the host directly into
the file is CRC'd correctly by the minion; `reset 0` restarts the firmware (Mailbox1 -> 0 -> 0xC0FFEE00)
while MRAM keeps its contents.

## 5. Protocol interpretation / deviations from docs/protocol.md

None intentional. Choices where the spec is silent:

* Response header `addr` echoes the request `addr` (spec only says "0 otherwise"; harmless).
* `erb_info.flags` = 0 (no flags defined yet).
* A header with bad magic/version or `len > 4096` gets a `BADREQ` response and the connection is
  dropped (a corrupt stream cannot be resynchronised). `READ`/`WRITE` with `len == 0` -> `BADREQ`,
  unknown op -> `BADREQ`, unknown `RESET` kind -> `BADREQ`.
* Register regions: the access is split into the largest of the sizes the region accepts (sysregs/SCCR:
  4 or 8, UART/PLIC: 4, ESRs at 0x80000000: 8) that divides both address and length; otherwise
  `SLVERR`. An unknown register offset inside a mapped region throws `memory_error` in sysemu and is
  reported as `DECERR` (matches "no target at that address"); PLIC writes to read-only words also
  come back as `DECERR` because sysemu signals them the same way.
* Accesses spanning two adjacent regions are split at the boundary; a hole between regions is `DECERR`.
* `RESET` kind 0 = `sys_emu::por_reset()`: system registers (reset cause POR) + SCCR stub reset, then
  the same cold-reset / configure / warm-reset sequence the constructor uses, so harts restart at the
  reset PC with the currently loaded ROM/SRAM/MRAM contents (ROM is not reloaded from the ELF, it is
  simply not wiped). ESR/PLIC/UART state is reset via `cold_reset`/`warm_reset` as far as sysemu models it.

## 6. Surprises / things to know

* `testLog` never clears its fatal flag, so `api_communicate::notify_fatal_error()` is called again for
  every later log line (and with an empty message - existing bug). The `SocketAgent` therefore reacts
  only when there is a hart to stop. Stand-alone sysemu ends the simulation on FATAL; with the socket
  the harts are taken offline (`become_unavailable`) and the socket keeps being served until a host POR.
* Erbium sysregs are 32-bit registers on a 64-bit stride and the PMA only allows 4-byte, 8-byte-aligned
  accesses from harts: Mailbox1 is at `0x02000070` (not `0x6c`).
* Hart 0 always comes out of reset running (`thread0_disable` resets to 0xFE); `-mins_dis` does not
  change that on Erbium. With no ELF it faults at once and is parked as described above.
* `hwinc/top.h` sizes the xSPI register block at 0x1C; the TRM SCCR map is 0x38 with 64-bit stride.
  The stub covers 0x40 bytes; `top.h` was not changed.
* The `-max_cycles` default (10 M) would stop a served emulator after a few seconds; `--api-socket`
  lifts it unless given explicitly.
* `sys_emu::sys_emu()` requires the api listener before construction, but `por_reset()` needs the driver:
  `main.cpp` calls `SocketAgent::set_emu()` after constructing `sys_emu`, and destroys `sys_emu` before
  the agent.
