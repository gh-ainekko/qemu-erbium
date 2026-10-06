# Proposal: host-loaded Kotama, xSPI boot control, separate UART console

Status: **proposal**, with a built/booted standalone baseline; host-loaded Kotama
and the proposed new commands/options below are **not implemented**.
Study date: 2026-10-06.

## Recommendation

Keep the existing separation of responsibilities:

- **xSPI** carries image upload, verification, CPU boot/reset control, and later
  application data/mailboxes.
- **UART** carries Kotama's existing console, including runtime diagnostics.
- Keep Kotama's Erbium binary unchanged initially. Correct missing sysemu reset
  semantics and supply a real bidirectional UART endpoint rather than inventing
  an emulator-only ELF-load RPC or replacing the console prematurely.

“Host” here means the **ARM64 Linux guest** acting as the SPI master, not the
x86_64 development machine. The development machine orchestrates QEMU/sysemu but
must not populate MRAM or pass Kotama to sysemu's `-elf` in the acceptance test.

| Function | Linux host interface | Emulation path |
|---|---|---|
| Image bytes | `/dev/mtd0` | Cadence OSPI → Erbium xSPI → shared MRAM |
| Boot/control registers | `/dev/erbium0` | xSPI → existing control socket → sysemu registers |
| Kotama console | `/dev/ttyAMA1` | second Versal PL011 → **separate** raw UART socket → Shakti UART |
| Linux boot/login console | `/dev/ttyAMA0` | existing QEMU stdio console; unchanged |

On hardware the last Kotama-console hop corresponds to the host UART wired to
Erbium RX/TX. This physical wiring must be confirmed for the target board.

## 1. What was studied and actually tested

Pinned source snapshots:

| Component | Revision |
|---|---|
| `usbarmory/kotama` | `9e5db018306f6dfa7905932c29c7e2e988e2d6bf` |
| `abarisani/tamago-go`, branch `tamago1.27.1-softfloat` | `a210dc3c39b40a46868d5101d78f2056a9b38826` |
| TamaGo, as pinned by Kotama | `9e72834d5757038516dddf4c3e402210130ecc7e` |
| tamago-example shell | `49c85ca96952` (Kotama's pinned Go module version) |
| Local sysemu | `4afc2c95f4f725b78daa020a9898a7458a0c9e5e` |
| Local core-et-erbium RTL | `afa22ae4d30ef5efe52ddf7a8ec7ddb55c3da21f` |

Built the experimental compiler with `GOMAXPROCS=2 ./make.bash`. Then built the
application using Kotama's Erbium recipe, with an isolated build cache:

```
GOOS=tamago GOARCH=riscv64 GOSOFT=1 GOOSPKG=github.com/usbarmory/tamago
-tags erbium_emu,tiny,linkcpuinit
-trimpath -ldflags '-T 0x40010000 -R 0x1000'
```

`GOSOFT` requires a dedicated cache: upstream explicitly warns that this variable
is not accounted for in Go build IDs. A stock Go compiler is not sufficient.

The resulting ELF was booted using our **existing**, unmodified sysemu binary,
with `-elf_load`, explicit `-reset_pc`, `-minions 0x1`, `-single_thread`, and
`-max_cycles -1`. Both UART file arguments named the same raw PTY slave; emulator
logs went to a different file. Sending `info\r` produced:

```
tamago/riscv64 (go1.27.1) • こたま
SoC ..........: Erbium (eb680000) @ 200 MHz (rv64cfimsux)
Minions ......: 1
Runtime ......: go1.27.1 tamago/riscv64 thread 0
RAM ..........: 0x40000000-0x41000000 (16 MiB)
Text .........: 0x40010000-0x400fa2a0 (936 KiB)
Data .........: 0x40250480-0x40285508 (212 KiB)
```

This proves binary/ISA compatibility and the current UART-backed shell baseline.
**It does not prove xSPI loading or host-controlled start.** Research artifacts
are local under `build/kotama-study/`; they are not yet release artifacts.

### ELF findings

The studied image is an ELF64 little-endian RISC-V `ET_EXEC`, about 3.6 MiB on
disk. Its SHA-256 is
`a08749b9b924be9b68f33443dfec2b481d905b096fd830ae2492219244a38f58`.

- `e_entry = _rt0_riscv64_tamago = 0x400716f0`.
- Lowest `PT_LOAD` address: **`0x4000f000`**, including the mapped ELF headers.
- Last segment ends at `0x40285508` (exclusive), including zero-fill.
- Three `PT_LOAD` segments; `p_paddr == p_vaddr` for each.
- Writable segment has a `0x23748`-byte zero-fill tail.

These numbers describe this build, not a permanent ABI. In particular,
**`0x40010000` is the text link address, not the entry point**, and `dd` of the
whole ELF to MRAM offset `0x10000` would be incorrect.

TamaGo reserves RAM at `0x40000000` with a 16 MiB size and sets its initial stack
near its upper end. Start with **one minion/thread**. `linkcpuinit` parks extra
harts; Kotama's advertised `smp` command is guarded by `sys_emu` and is not
available in the `erbium_emu` build. Multi-minion application execution is a
separate milestone, not implied by the upstream launch mask `0xff`.

## 2. Host ELF loader and hardware-faithful boot sequence

Add a small static Linux userspace loader, preferably as an extension to
`erbctl`. Do not put ELF parsing in the kernel driver. Proposed UX:

```
erbctl load /firmware/kotama.elf --verify --start
erbctl console /dev/ttyAMA1
```

Names/options are illustrative, not currently supported. The loader should:

1. **Validate the entire image before touching hardware.** Require ELF64 LE,
   RISC-V, static executable, bounded/complete program headers and file ranges,
   `p_filesz <= p_memsz`, overflow-safe MRAM bounds, and an entry inside an
   executable load segment. Initially require matching physical/virtual
   addresses, disallow conflicting overlapping segments, and reject dynamic
   linking/relocations. Use ELF `e_entry`, verifying its `_rt0` correspondence
   at package-build time rather than requiring symbols at load time.
2. Attach the console, stop other users of the device, and assert **CPU warm
   reset hold**. Confirm the write and honor hardware reset settling; do not
   equate a readable request bit with an independent CPU-stopped indication.
3. Disable all threads while held. Copy each `PT_LOAD` segment to MRAM via the
   MTD character device; handle short writes and zero `p_memsz-p_filesz` tails.
   In particular, repeated loads must not retain old BSS state. Respect the
   driver's 8-byte RMW and 4-KiB frame boundaries rather than bypassing them.
4. Read back and compare the image and zero-filled ranges before release. A
   failure leaves the CPU held and produces a nonzero command status.
5. Set `MINION_BOOT` from `e_entry`, select only minion 0/thread 0, and release
   warm reset. Wait for the UART banner and a known `info` response with a
   deadline; make boot failure explicit.

Address domains and control values:

| Item | CPU/backend address | Linux xSPI address | Value/use |
|---|---:|---:|---|
| MRAM | `0x40000000..0x40ffffff` | `0..0x00ffffff` | image offset = CPU address − `0x40000000` |
| System `SoftReset` | `0x02000028` | `0x40000028` | `0x6` hold; `0x4` release |
| `MINION_BOOT` | `0x80d00018` | same | 64-bit ESR, address bits `[47:0]` |
| `THREAD0_DISABLE` | `0x80f40240` | same | `0xff` all off; `0xfe` minion 0 on |
| `THREAD1_DISABLE` | `0x80f40010` | same | `0xff` all off |

SoftReset bit 1 is **active-high persistent CPU warm reset**, but bit 2 is
**active-low MRAM reset**. Consequently, writing `0x2` instead of `0x6` would
also assert MRAM reset. Use the defined register semantics, not an unqualified
“reset” helper. System registers use 32-bit values on 8-byte strides; ESRs need
full 64-bit accesses (`erbctl mem32` is not a generic ESR helper).

This is grounded in the RTL testbench's own frontdoor sequence: disable
minions, set `minion_boot`, set enable masks, release warm reset. The CPU-register
bus remains accessible during warm reset. The testbench normally asserts
**TestMode** to hold the CPU at startup. We must model that documented startup
condition (or another agreed boot-ROM/strap profile), not silently run empty ROM
until the emulator happens to fault. The hardware board's actual strap/ROM
contract is a remaining question.

**Do not finish upload with `erbctl reset`/99h.** It is whole-chip reset, not a
start command: the existing backend resets ESRs and reapplies its CLI boot PC
and hart configuration. MRAM persists, but the just-programmed boot settings do
not. Also preserve the active xSPI transfer mode during the warm-reset sequence.

A userspace loader can initially use existing MTD/MEM ioctls, with one
cooperating loader and exclusive device ownership. Before general multi-client
use, add proper boot-session serialization; a per-ioctl mutex does not serialize
an entire upload/start transaction.

### Required sysemu fix before that sequence works

`SysregsEr::write_register(SOFT_RESET)` currently **only stores a field**. It
neither holds nor restarts CPUs. Thread disable/re-enable alone resumes the old
PC; changing `MINION_BOOT` alone does not redirect an executing hart.

Implement warm hold/release consistent with RTL, including:

- startup-held state for the selected TestMode/boot profile;
- no instruction execution while held, even if disable masks are changed;
- continued xSPI socket service and peripheral progress while held/asleep;
- sampling the **new** boot PC on release, with the appropriate hart reset state;
- separate whole-chip, CPU-only, and xSPI-only reset semantics;
- no hidden reapplication of CLI defaults on ordinary warm release.

Existing `begin_warm_reset()` samples PC on assertion, whereas
`end_warm_reset()` merely recomputes enables. Simply connecting these helpers to
the bit would miss a boot-PC change made while held. Tests must catch that.

The backend already permits `--api-socket` without `-elf`, but today that means
hart 0 can fault in empty ROM before its socket listener takes it offline. That
is service liveness, **not** a valid boot/reset implementation.

## 3. UART is a separate serial cable, not xSPI register polling

Kotama assigns its shell to `erbium_emu.UART0`, the **Shakti UART** at CPU
`0x02004000`. Runtime `printk` also writes there. Preserve both paths initially.

The Versal machine already has two PL011 devices and DT aliases. UART0 is the
Linux console; UART1 at `0xff010000` is available as `/dev/ttyAMA1`. No new QEMU
UART device or host-side Shakti Linux driver is needed.

Proposed wiring:

- host ttyAMA1 write → PL011 UART1 TX → raw socket → Erbium Shakti RX → Kotama;
- Kotama → Shakti TX → raw socket → PL011 UART1 RX → host ttyAMA1 read.

Use a **new UART socket**, not the framed xSPI register-protocol socket. For
example, a proposed sysemu `--uart-socket PATH` listener and QEMU's second serial
backend:

```
-chardev socket,id=kotama-uart,path=/tmp/kotama-uart.sock,reconnect-ms=1000
-serial chardev:kotama-uart
```

Keep the existing `-serial mon:stdio` **first** for Linux UART0. The UART socket
option in sysemu does not exist yet. The protocol remains a raw bidirectional
byte stream, never emulator logs or a QEMU monitor.

### Why xSPI access to the UART is not a console solution

Host xSPI address `0x40004000` does reach the same UART's MMIO registers, but:

- reading RX consumes bytes sent **to** Erbium and competes with Kotama;
- writing TX sends bytes **out of** Erbium, not into Kotama's shell;
- reading TX does not retrieve transmitted console output.

That accesses the CPU-facing side of the UART, not the peer's wire endpoint.
Large bursts/RMW also have inappropriate side effects for FIFO registers.

### Existing transport shortcomings to address

The present backend offers `-uart_rx_file`/`-uart_tx_file`, not socket connect or
listen. A raw PTY plus a socket relay is enough for a prototype, and a raw PTY was
used successfully for the standalone baseline. Do not pass a socket pathname to
these file options: they use `open()`, not `connect()`.

For a dependable release, use one owned nonblocking full-duplex connection:

- RX currently greedily drains readable input even after its **16-byte FIFO is
  full**, discarding the remaining bytes. A short `info` works; a pasted long
  command can fail. Do not mistake this for a Kotama shell bug.
- TX currently removes a byte before calling `write()` and ignores its result.
  Retryable errors lose data; a blocking descriptor can stall all simulation.
- Add bounded transport buffering, respect available RX FIFO space, retain TX
  bytes until delivery, and handle disconnect/reconnect explicitly. Simulated
  baud/overrun behavior should be deliberate, not an artifact of UNIX socket
  batching. State whether a test uses reliable byte-stream or timed-line mode.
- Keep emulator diagnostics separate from UART bytes; avoid inherited stdin,
  stdout, canonical terminal processing, CR/LF translation and XON/XOFF.
- Supply a termios-aware console helper using raw mode/no echo; the minimal
  initramfs currently does not expose a `stty` symlink.
- Connect before releasing CPU reset so early boot output is observable.
  Serialize console input with reloads to avoid feeding a new image old commands.
- CPU-only warm reset must not implicitly reset the UART or disconnect its
  cable. Whole-chip reset must clear UART state appropriately; the current
  backend POR path does not reset the Shakti object. Retain socket ownership
  across resets, with explicit queue/stale-data handling.

**Baud qualification:** the current TamaGo driver enables the UART but does not
program baud/framing. QEMU/sysemu stream attachment does not enforce matching
physical baud. A successful virtual console therefore does not validate board
clock/divider/baud setup. Confirm it before hardware deployment; the RTL divider
also differs from its `2*count` comment (steady state is `2*(count+1)`).

## 4. If the real board only exposes xSPI

Then an unchanged UART-console binary cannot provide its interactive shell over
that link. Add a separate Kotama/TamaGo **host-mailbox console profile**:

- two bounded MRAM/SRAM rings with owned producer/consumer indices;
- mailboxes/interrupts as notifications, not one-register-per-character traffic;
- an `io.ReadWriter` implementation for the shell **and** a matching runtime
  printing hook (`linkprintk`) so panics/logs follow the same transport;
- explicit reserved memory excluded from TamaGo's heap/stack allocation;
- cache/coherency, memory barriers, wraparound, reset and session/version rules.

Do not choose an arbitrary address inside the current 16 MiB region: TamaGo
considers that region available RAM. Prefer this only if the board lacks the
UART path or we explicitly want an xSPI-only product. Do not emulate a fictitious
UART FIFO reachable from the wrong side to keep a binary unchanged.

For later application workloads, shared-memory buffers and mailbox signaling
can coexist with the UART console; UART should not carry bulk model/input data.
A firmware-ready mailbox ABI would be a small deliberate Kotama addition, not
something the current binary already implements.

## 5. Proposed implementation order and acceptance criteria

1. **Pin and package the standalone baseline.** Separate build target for the
   TamaGo compiler/Kotama image, isolated soft-float cache, recorded ELF entry,
   segment metadata and checksums. Keep compiler building opt-in initially.
   Ship `kotama.elf`; include it in the Linux guest initramfs for the first test.
2. **Fix architectural reset/start behavior.** Small sysemu patch with tests for
   held startup, socket liveness, boot-PC changes during hold, mask gating,
   MRAM preservation, and repeat warm starts. No “LOAD_ELF” backend RPC.
3. **Expose a robust serial endpoint.** Native UART socket plus QEMU UART1
   connection and host console helper. Exercise >16-byte input, slow readers,
   retryable writes, reconnect, reset and log separation.
4. **Add the host ELF loader and end-to-end test.** Start with empty MRAM and
   backend CPU held, **no Kotama `-elf` argument**. Inside Linux, load/verify/start
   through MTD/control ioctls and interact through ttyAMA1. Assert banner, `info`,
   `uptime` and `help` replies. Then perform a full reload/start and repeat.
5. **Harden and automate.** Reject truncated/wrong-architecture/out-of-range ELF
   files before modifying hardware. Failed verification must not run the CPU.
   Test image identity/reload zero-fill, disconnection, timeouts and xSPI errors;
   preserve existing qtests/mailbox smoke tests. Export updated source patches
   and run the complete flow in pristine build and binary-only Ubuntu containers.

Do not make Kotama's `reset` shell command an initial success criterion: the
current TamaGo warm-reset request can assert a persistent CPU hold, and the
current backend does not implement it. Agree on the firmware/host reset contract
before enabling that as a self-contained “reboot” command.

The **first milestone is successful host-driven image upload and execution of
an unchanged Kotama ELF, with a working host-accessible console**. Physical
UART baud qualification, xSPI-only console, SMP, application RPC and boot-ROM
image formats remain separate decisions.

## 6. Source evidence

Upstream primary sources (pinned rather than floating branches):

- [Kotama build/launch](https://github.com/usbarmory/kotama/blob/9e5db018306f6dfa7905932c29c7e2e988e2d6bf/run.sh),
  [dependencies](https://github.com/usbarmory/kotama/blob/9e5db018306f6dfa7905932c29c7e2e988e2d6bf/go.mod),
  [Erbium console](https://github.com/usbarmory/kotama/blob/9e5db018306f6dfa7905932c29c7e2e988e2d6bf/cmd/erbium_emu.go).
- [TamaGo Erbium platform](https://github.com/usbarmory/tamago/tree/9e72834d5757038516dddf4c3e402210130ecc7e/soc/aifoundry/erbium),
  [Shakti UART](https://github.com/usbarmory/tamago/blob/9e72834d5757038516dddf4c3e402210130ecc7e/soc/aifoundry/uart/shakti.go),
  [runtime output](https://github.com/usbarmory/tamago/blob/9e72834d5757038516dddf4c3e402210130ecc7e/board/aifoundry/erbium_emu/console.go).

Local source evidence (paths relative to the workspace):

- `ext/core-et-erbium/regblocks/systemrdl/system.rdl:51–56`: reset fields/polarity.
- `ext/core-et-erbium/tb/env.py:99–101,213–247` and `tb/test_elf.py:57–74`:
  warm-reset values and frontdoor boot sequencing.
- `ext/core-et-erbium/prcm/verilog/power_aware_reset_ctrl.v:16–43`,
  `prcm/verilog/prcm_et.v:226–257`, and
  `erbium_digital/verilog/erbium_digital_et.v:708–710,862–871`:
  hold/release wiring, CPU-register accessibility, TestMode startup hold.
- `et-platform/erbium-hal/include/hwinc/system.h:253–313` and
  `include/hwinc/esr.h:2183–2207,2653–2676,2873–2896`: register definitions.
- `et-platform/sw-sysemu/devices/sysregs_er.cpp:157–159`,
  `system.cpp:70–99`, `processor.cpp:1799–1824`, and
  `sys_emu/sys_emu.cpp:484–547`: reset implementation gaps.
- `et-platform/sw-sysemu/devices/shakti_uart.h:148–155,239–302`,
  `sys_emu/sys_emu.cpp:447–466`, and `memory/erbium/main_memory.cpp:61–65`:
  UART I/O, queues and reset handling.
- `ext/qemu/hw/arm/xlnx-versal.c:288–290,1020–1058`: existing two PL011s,
  chardev binding and serial DT aliases.
- `ext/core-et-erbium/ip/erbium_noc/rtl/erbium_noc_top.sv:641–665`:
  xSPI-to-CPU address translation.

RTL verification limit: some CPU/UART submodule implementation trees are not
populated in the local RTL checkout. This study checked top-level/reset wiring,
register definitions and testbench intent; it did not run a new RTL simulation.
