# Real-chip host console connectivity: UART versus bus/mailbox transport

Design study: 2026-10-07. Recommendation, not an implemented feature.

## Recommendation

Wire the actual UART to a host UART for bring-up, diagnostics and the existing
Kotama console. Retain xSPI for verified loading, boot control and bulk data.
If board pin budget allows, also route one spare Erbium GPIO output to a
host interrupt-capable input for future bus-based services. Keep UART test points.

A bus console is a valid alternative when pin availability, throughput or system
integration justifies firmware/host-driver work. Implement it with two shared
memory queues, not by treating the UART's CPU-facing registers as its serial
peer. A system-register interrupt already provides host-to-Erbium notification;
two new GPIO wires are not inherently necessary.

These findings describe checked-in digital RTL, not a verified package pinout,
board schematic or tape-out revision. Confirm voltage domains, package bonding,
host controller capabilities and silicon revision before schematic sign-off.

## Evidence baseline

RTL: `openhwfoundation/core-et-erbium`, commit
`afa22ae4d30ef5efe52ddf7a8ec7ddb55c3da21f`, locally `ext/core-et-erbium/`.
Paths below are relative to that directory unless specified otherwise.
TamaGo source studied: `9e72834d5757038516dddf4c3e402210130ecc7e`.

Important primary sources:

- `erbium_digital/verilog/erbium_digital_et.v:592–667`: GPIO edge detector/mux;
  UART TX on GPIO9, RX on GPIO10.
- Same file `776–785,1376–1407`: interrupt sources and integrated UART ports.
- `regblocks/systemrdl/system.rdl:23–49,156–184`: configuration, doorbell,
  mailboxes and GPIO registers.
- `regblocks/verilog/System_Reg.sv:998–1049`: actual read-clear arbitration.
- `regblocks/systemrdl/top_{cpu,xspi}_mm.rdl`: address-domain translation.
- `ip/uart/verilog/uart.v:1106–1132,1481–1500,2466–2473`: FIFO depths and
  implemented bus-side enqueue/dequeue/decode.
- `ip/uart/bsv/uart_cfg_regs.bsv` and `RS232_modified.bsv`: UART behavior,
  conditional modem support and transmitter output enable.
- `ip/xspi/bsv/{xspi.bsv,xSPITypes.bsv}`: SPI/HyperBus framing and AXI bridge.
- `doc/{interrupts,prcm,cpu_subsystem}.md`: routing, clocks and cache attributes.
  Some prose/register metadata is stale; resolve conflicts against built RTL.

The CPU submodule is not populated in the inspected RTL checkout. Top-level
interrupt wiring is visible, but internal CPU IPI/PLIC behavior is not completely
RTL-verified by this study. Emulator behavior is not silicon proof.

## 1. Direct UART

Connection, using logical GPIO numbers rather than package ball numbers:

```
Host UART TX --> Erbium GPIO10 / UART_RX
Host UART RX <-- Erbium GPIO9  / UART_TX
Ground       --- compatible ground/reference
```

`SystemConfig.uart_enable` (bit 6) selects the UART pinmux. It resets disabled.
The host-visible configuration address is `0x40000008`; the CPU view is
`0x02000008`. Preserve unrelated configuration bits. This is a software pinmux
setting, not a separate UART-enable strap found in the top-level port list.

Advantages:

- Existing Kotama shell and runtime printk already target the Shakti UART;
  no new console protocol is required. Physical-board clock/baud/early-init
  work may still be necessary: an `erbium_emu` image is not a certified board BSP.
- Linux drives its own UART using its normal driver; it does not need to own
  Erbium's Shakti MMIO registers.
- Each receiver has its own local UART interrupt. No separate cross-board IRQ
  wires are needed for ordinary UART reception.
- Independent of xSPI transfer-mode changes and shared-memory cache handling.
  Particularly useful for diagnosing a broken host bus. Still depends on
  functioning Erbium clocks, UART initialization and executing firmware.

Hardware checks:

- Verify pad voltage/tolerance and use level translation where needed. Do not
  mistake UART protocol terminology for RS-232 electrical-level compatibility.
- Configure matching framing/baud using the actual peripheral clock. The clock
  tree uses a trimmable ring oscillator and dividers; CPU frequency is not
  automatically the UART clock. Validate baud accuracy across intended operation.
- GPIO9 output enable follows UART `SOUT_EN`; it can be deasserted at idle.
  Verify actual pad behavior and idle-high bias at the host receiver.
- The integrated ports do not expose RTS/CTS. Generic BSV has conditional modem
  code, but it is not evidence that this chip instance supports those pins.
- The RX FIFO holds 16 entries. For 8N1 at 115200 baud, an empty 16-character FIFO
  represents only about 1.39 ms of storage, less when partly occupied. Polling
  that works for typing may lose pasted/binary input. Plan an interrupt-driven
  software RX ring and/or protocol pacing/credits; extra GPIO can implement
  software readiness but does not add automatic CTS gating inside the UART.

A USB-to-logic-UART adapter is useful for independent bring-up. If the host has
no native UART, an external SPI/I2C-to-UART bridge is another board option; the
bridge's TX/RX connect to Erbium's real RX/TX. This is not remote access to
Erbium's own UART registers. NXP SC16IS740/750/760 is an example architecture,
not a selected BOM part; check voltage, throughput, availability and Linux support
for the actual candidate. Its datasheet was checked during this study.

## 2. What host access to the UART registers actually means

Both host and CPU reach the same UART. Host xSPI base: `0x40004000`; CPU base:
`0x02004000`.

| Host operation | Actual effect |
|---|---|
| Write TX, offset `0x08` | Add output to the serial transmitter |
| Read RX, offset `0x10` | Consume received serial data, competing with firmware |
| Read TX | No supported transmitted-data FIFO retrieval operation |
| Write RX | No supported serial-receiver injection operation |

Thus register access can let the host use Erbium as a **remote UART peripheral**
to talk to a third device, provided ownership is exclusive. It does not make the
host the serial peer of software running on Erbium. Interrupt wires change
notification, not data direction or FIFO ownership.

Physical TX-to-RX loopback would circulate output back into the one shared RX
FIFO. Host and firmware would still need serialized ownership and echo handling;
it is not two independent bidirectional endpoints. No integrated modem loopback
bit was found. Reject this as the normal console architecture.

A future dual-ended hardware FIFO could preserve the target UART-style API while
providing host RX-injection/TX-extraction registers and proper status/interrupts.
That requires RTL/new-silicon work, not a GPIO addition to the existing chip.
An external MCU/FPGA UART bridge is possible without changing silicon, but adds
hardware and firmware and still uses the physical serial link.

## 3. Bus console: viable with zero, one or two IRQ wires

Proposed design: two single-producer/single-consumer queues in reserved shared
memory, one per direction. Firmware routes console input/output through them;
Linux initially exposes a userspace PTY bridge, optionally a dedicated TTY driver
later. A TTY name would be a proposed interface, not an existing device.

Notification variants:

| Variant | Extra signal wires beyond existing bus | Tradeoff |
|---|---:|---|
| Poll both queues | 0 | Easiest protocol prototype; latency/polling cost; sleeping target needs a wake strategy |
| Register doorbell to Erbium; host polls | 0 | Target can use interrupts; host still polls |
| Register doorbell to Erbium; GPIO IRQ to host | 1 | Preferred interrupt-driven bus-console design |
| GPIO IRQ in both directions | 2 | Useful if host cannot address high MMIO registers; additional mux/edge-handshake work |

### Existing host-to-Erbium doorbell

Enable `SystemConfig.sys_interrupt_enable` bit 0, then write bit 0 of
`SysInterrupt` (host `0x40000020`, CPU `0x02000020`). Top-level RTL routes their
AND to PLIC source 4. Firmware must configure the relevant PLIC context and CPU
interrupt enables. `tb/test_interrupt.py` exercises this path through xSPI.

`Mailbox0/1` are ordinary 32-bit storage at host `0x40000068/70` and CPU
`0x02000068/70`. Writes do **not** automatically ring the doorbell.

`SysInterrupt` is **clear-on-read**, including GPIO pending state. Only the
intended interrupt handler should acknowledge it. Do not use a write-then-read
verification helper on this register: current `erbctl mem32 ADDR VALUE` performs
that readback and can consume the very doorbell just written. Partial writes
through the generic driver also use eight-byte RMW; use deliberate aligned,
write-only MMIO access rather than an accidental read-clear operation.

The CPU also documents a targeted IPI trigger at `0x80f40090` and clear at
`0x80f40098`. It targets machine software interrupts, not PLIC, and is implemented
in sysemu. Treat it as an alternative needing CPU/silicon verification and
coordination with runtime use of IPIs, not the first system-doorbell choice.

### Erbium-to-host notification

Firmware drives a spare GPIO connected to a host interrupt-capable input. The
inspected top-level has no dedicated automatic mailbox-to-host IRQ output.
Prefer a persistent pending indication on a level-capable host input, with a
specified acknowledgment/recheck protocol; do not rely on a short pulse. Handle
stuck IRQ, target reset and lost/coalesced notifications with bounded recovery.

GPIOs are multiplexed with I2C/SPI/QSPI/UART. GPIO3–6 are claimed by SPI at reset;
GPIO0 has oscillator/test interactions. Choose pins from the actual schematic.

For the optional reverse GPIO wire, the available RTL detects **both input
transitions**, latches an aggregated pending bit and routes it to PLIC source 6.
Contrary to some prose, it is not rising-only or a configurable level detector.
Reading `SysInterrupt` clears it; read-clear wins over a simultaneous hardware set.
Short-pulse/CDC behavior needs validation. Use queue sequence state and an explicit
acknowledgment/recheck protocol, not one pulse per byte.

No claim here that these interrupts wake a fully power-gated chip. Verify the
particular WFI/sleep/power-domain state and its wake paths separately.

### Shared-memory correctness is the substantial work

- MRAM: 16 MiB, CPU `0x40000000`, host offset `0`. Reserve the queue region in the
  firmware memory layout and loader contract. Space beyond the last ELF segment
  is not necessarily free: TamaGo uses memory for its heap and stack.
- SRAM: 4 KiB shared system RAM at CPU `0x0200c000`, host `0x4000c000`, is another
  candidate if boot/runtime ownership permits. It is not a private L1 scratchpad
  and is not automatically uncached.
- CPU caches/interconnect must not be assumed coherent with xSPI. Use verified
  uncached access or correct cache clean/invalidate plus ordering. `volatile`, a
  fence or an interrupt alone does not make stale cache lines disappear.
- Separate independently owned indices onto appropriate cache lines and avoid
  sharing an eight-byte driver RMW granule. Define widths, endian and wrap rules.
- Complete/publish payload before producer index, then doorbell. Verify external
  bus completion/ordering, not just chip-select release. The xSPI RTL buffers AXI
  writes; the current driver returning from a SPI transfer is not itself a proof
  of visibility to a cached running CPU.
- Use queue capacity/credits, session generations, reset handshakes and explicit
  overflow behavior. MRAM persistence means stale indices can survive reboot.
- Notifications are hints; queue state is authoritative. Test enqueue versus
  acknowledgment races, coalesced/lost IRQs and reset at every publication stage.
- Route both shell I/O and early/runtime printk if replacing UART. A shell-only
  replacement silently loses part of the diagnostic channel. Keep early UART
  fallback where possible; never indefinitely block panic output on a dead host.

## 4. HyperBus versus the current emulator's xSPI framing

The target RTL includes a HyperBus CA decoder, not merely HyperBus-named pads:
`xSPITypes.bsv:94–105`, `xspi.bsv:109–113,728–774`. Its straps select HB for `00`,
D8 for `01`, S4 for `10`, and S1 for `11` (`xspi.bsv:791–820`).

The current QEMU model implements SPI-style xSPI framing, **not HyperBus CA**.
Hardware bus-console feasibility and emulator HyperBus support are separate
questions. The conceptual mailbox design applies to either framing if the real
host controller can address the relevant memory/register windows.

In particular, a controller limited to a small MRAM aperture may not reach
`0x40000020`; in that case a host-to-Erbium GPIO or polling may be needed.
System/UART MMIO uses target **memory transactions**, not the xSPI-local SCCR
register-command address space. Verify addressing with the chosen controller.

## 5. Other options and their place

- **Native UART:** default production/bring-up console recommendation.
- **USB serial or external SPI/I2C UART bridge:** useful when native host UART is
  unavailable; preserve the target's normal UART console.
- **Shared-memory console:** good for integrated services and bandwidth; most
  software/protocol work, but can avoid serial pins and baud constraints.
- **GPIO readiness with normal UART:** possible software flow control, separate
  from a bus console. Neither direction magically gains hardware CTS.
- **Half-duplex/single-wire serial:** possible only with suitable electrical
  direction control and arbitration; asynchronous logs can collide with commands.
  Not a drop-in replacement for the existing full-duplex console.
- **GPIO bit-banging:** fallback for a demonstrated UART/pinmux problem, not the
  preferred use of CPU time or timing budget.
- **JTAG/debugger console:** recovery/development tool with firmware/debugger
  integration and mux constraints; not assumed to be a ready production console.
- **New RTL dual-ended FIFO:** attractive for a future chip/FPGA revision,
  impossible to add to existing silicon merely through software interrupts.

## 6. Proposed next implementation milestone

1. Confirm board pin/voltage/clock/reset contract and choose native UART versus
   an external serial bridge. Route an optional outgoing host-IRQ GPIO if useful.
2. Connect Linux guest UART1 (`ttyAMA1`) to the backend Shakti serial endpoint,
   independently of xSPI and Linux's UART0 boot console. Use an owned raw socket
   or supervised PTY relay; keep emulator logs out of the byte stream. The current
   FD transport already has backpressure fixes from the separate SmallVM work.
3. Add raw terminal handling, cleanup, exclusive ownership and bounded reconnect/
   buffer policy. Attach capture before releasing Erbium reset to retain the banner.
4. Acceptance: empty MRAM, guest `erbctl load --start`, then issue Kotama `info`
   through **guest UART1** and read the response there. No emulator ELF preload
   and no development-host-only shortcut for the acceptance console.
5. Audit RTL/HAL/emulator IRQ fidelity before calling this full hardware support:
   actual raw/mask/status offsets are `0x48/0x50/0x58`, whereas the current HAL/
   emulator puts interrupt enable at `0x30` (actual IQC). Built UART raw interrupt
   acknowledgment uses XOR, not unconditional W1C. Reconcile and regression-test
   against RTL rather than copying stale generated headers.
6. Test bursts past 16 bytes, full-duplex traffic, slow readers, reset/reconnect,
   byte transparency, RX overrun, error/interrupt acknowledgment and console
   escape/terminal restoration. A reliable backpressured Unix stream is not a
   baud-clocked wire: physical overflow, baud mismatch, break and framing behavior
   require additional modeling and board tests. Do not infer electrical fidelity
   from successful socket/PTY tests.
7. If a bus console is selected, prototype the two queues with polling first,
   then implement/verify system-doorbell and GPIO emulation and race-safe handlers.
   Current sysemu does not implement the needed SysInterrupt/GPIO register paths.
   Do not hide that missing hardware behavior behind a private console RPC.

This keeps the next step small and hardware-aligned while leaving a clear route
to a bus-native console if product constraints justify it.
