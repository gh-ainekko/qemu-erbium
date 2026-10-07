# Direct Linux-host ↔ Erbium UART console

Implemented wiring option 1 from `uart-host-connectivity.md`. This is an
independent serial-peer connection, not an xSPI UART-register console or a
mailbox console protocol. Linux still uploads/verifies ELF images through xSPI.

## Hardware correspondence

| Real board | Emulation |
|---|---|
| Host UART TX → Erbium GPIO10/UART_RX | Linux ttyAMA1 → Versal PL011 UART1 TX → raw UNIX stream → Shakti RX FIFO |
| Erbium GPIO9/UART_TX → host UART RX | Shakti TX FIFO → raw UNIX stream → PL011 UART1 RX → Linux ttyAMA1 |
| Erbium local UART interrupt | Shakti masked interrupt → Erbium PLIC source 3 → RISC-V hart |
| Host local UART interrupt | Existing PL011/GIC interrupt → Linux serial driver |
| Separate host bus connection | Existing OSPI/xSPI control socket and MRAM mapping; unchanged |

QEMU already supplies PL011 UART1 at `0xff010000` and the `serial1` DT alias;
no new QEMU UART device or synthetic guest character device is needed. UART0
at `0xff000000`, `ttyAMA0`, remains Linux's boot/login console. Never configure
UART1 as a Linux kernel console or run a getty there.

The raw stream represents TX/RX crossed as on a board. It carries only target
serial bytes, never emulator diagnostics, a QEMU monitor, or xSPI framing. No
additional GPIO, remote FIFO injection register, or console RPC is introduced.

## Quick start

After `J=2 ./bootstrap.sh` (or extracting a complete binary distribution):

```sh
scripts/run-uart-test.sh       # hermetic IRQ/WFI echo and repeated host load
scripts/run-uart.sh            # interactive Linux, new empty MRAM, CPU held
```

The interactive runner creates a fresh temporary board session. In Linux, boot
and connect to the supplied echo fixture:

```sh
erbctl console /dev/ttyAMA1 --load /firmware/uart-smoke.elf
```

For your own ELF, first embed it in the **Linux host** initramfs:

```sh
ERBIUM_ELF=/absolute/path/application.elf J=2 scripts/build-all.sh linux
scripts/run-uart.sh
```

Then, inside Linux:

```sh
erbctl console /dev/ttyAMA1 --load /firmware/host-payload.elf
```

The console attaches/configures UART before its loader child uploads, verifies
and releases Erbium. It relays output while loading, so the startup banner is
not lost. Loader diagnostics go to stderr; stdout is serial output only. `-d`
selects the control device and `--mtd` selects its matching MRAM for `--load`.
Without `--load`, `erbctl console /dev/ttyAMA1` simply attaches to an existing
session and does not open the xSPI control device.

**Ctrl-] exits interactive console mode.** Ctrl-C is passed to the remote program
when local stdin is a terminal in raw mode. Signals sent to the process restore
terminal settings. Binary pipe input has no escape-character interpretation.
EOF drains queued output and waits for a 500 ms reply-quiet period, bounded by a
3 s hard deadline; incomplete draining is an error, not a success guarantee for
arbitrarily late replies. Console closes do not reset a running target. If an
in-progress loader is cancelled, its normal hold-on-failure cleanup is attempted;
SIGKILL or permanently stuck control I/O cannot guarantee recovery.

The console sets raw 8N1, 115200 by default (`--baud N` selects another supported
rate), disables software/hardware flow control and does not flush received UART
bytes. It uses cooperative `flock` and TIOCEXCL; these cannot evict an already-open
reader or prevent all privileged opens. Termios and modified descriptor flags
are restored on normal exit, managed signals and errors, not SIGKILL/power loss.

`KEEP_UART_LOGS=1` retains runner session files. Failures always retain them and
print their location. The runner owns/reaps both emulator processes, including
on PID-only SIGTERM. Its temporary MRAM is removed after a successful session by
default; use the manual wiring below when persistent MRAM is desired.

## Manual wiring / persistent MRAM

Choose an owned private directory and unused socket pathnames. In one terminal:

```sh
mkdir -m 700 /tmp/my-erbium
truncate -s 16M /tmp/my-erbium/mram.img  # fresh image only; do not truncate valuable existing data
build/sw-sysemu/erbium_emu -single_thread -minions 0x1 --start-held \
  --api-socket /tmp/my-erbium/control.sock --mram-file /tmp/my-erbium/mram.img \
  --uart-socket /tmp/my-erbium/uart.sock > /tmp/my-erbium/backend.log 2>&1
```

Use `bin/erbium_emu` in a binary distribution, or `dist/bin/erbium_emu` after a
complete source build. In another terminal:

```sh
scripts/run-linux.sh --backend /tmp/my-erbium/control.sock \
  --mram /tmp/my-erbium/mram.img --uart-socket /tmp/my-erbium/uart.sock
```

`run-linux.sh` keeps `-serial mon:stdio` first and connects a **second** serial
backend with `socket,id=erb-uart,...,reconnect-ms=1000`. The UART socket is distinct
from the `erbium-xspi` chardev. Do not reuse one socket for both channels.

The backend accepts one UART client; extra clients are closed. File-based UART
RX/TX options cannot be combined with `--uart-socket`. Existing socket/file paths
are never unlinked to make room for the listener: remove an old socket only after
confirming its owner is stopped. Normal shutdown removes only the owned socket.

EOF/half-close/error ends a serial transport session and permits a new peer.
Internal UART FIFOs are retained across peer reconnect; data in the old kernel
socket queues may be lost. This is not an acknowledged, lossless reconnect
protocol. xSPI connection lifecycle remains independent. The endpoint remains
serviceable with held CPUs and while firmware sleeps in WFI; it never borrows
stdin/stdout for serial data in socket mode.

## RTL alignment and scope

The model now uses the compiled UART RTL rather than stale generated HAL fields:

- Correct IRQ RAW/MASK/STATUS offsets `0x48/0x50/0x58`; `0x30` is IQC.
- Correct reset values, native-width bus read replication and register access
  permissions. Host 64-bit bus accesses no longer split one UART register into
  two unrelated accesses.
- Sticky RAW and threshold status, observed-bit XOR RAW acknowledgment, proper
  TX-not-full IRQ polarity, and clearing the PLIC line when masks are removed.
- UART MMIO/internal FIFO access remains available independently of pad mux.
  GPIO9/10 routing controls serial arrivals/output, not CPU register access.
- UART resets on whole-chip/peripheral reset, not CPU warm hold/release,
  CPU-only cold pulse or xSPI-only reset. The listener/connection survives resets.
- CPU PMA recognizes the mapped 4-KiB UART aperture, so real firmware can access
  the interrupt registers above the stale HAL's former `+0x44` endpoint.

**This is functional byte-stream emulation, not electrical/bit-timed UART
co-simulation.** Baud/framing/qualification register values are modeled, but baud
mismatch, parity/framing errors, BREAK, oscillator drift, real-wire RX overruns,
TX shift-register timing and electrical output-enable/pulls are not simulated.
The finite 16-byte FIFOs backpressure host streams rather than inventing physical
RTS/CTS. With no peer, queued TX can fill and firmware can wait for space; a real
unconnected transmitter would keep shifting. Do not infer hardware losslessness
or service-latency margins from this mode.

While pads sample disabled, incoming stream bytes are discarded in bounded work
and disabled TX advances without emitting to the peer. Stream bytes not sampled
until after an enable transition may be accepted later: the model has no bit
arrival timestamps. It does not discard valid first input after an enable ACK.
Chip reset clears device FIFO state, not bytes still queued in external streams.

Some vendor HAL UART metadata remains stale; new firmware should follow the
compiled RTL and the IRQ smoke fixture, not assume the old `InterruptEn=0x30`
layout is correct. Real boards still require verified voltage, pinmux, clock,
baud, idle bias and software flow-control/service-latency design.

## Acceptance and regressions

Default CI is hermetic and requires no TamaGo compiler or network fetch for a
Kotama binary:

- Empty MRAM, held CPU, **no backend `-elf` preload**.
- Linux opens/configures its real UART1 before invoking `erbctl`.
- Guest loader validates, uploads and verifies the fixture through xSPI.
- Firmware prints a fresh banner; RX echo runs **only in a UART interrupt handler**
  through PLIC source 3, with the main loop in WFI.
- Binary data includes NUL/CR/LF/XON/XOFF/0xff and bursts beyond FIFO capacity.
- Two load/start rounds retain the same Linux tty and serial connection; stale
  bytes, missing bytes, echoed protocol debris, timeouts or loader failures fail.

Additional backend fixture tests require actual IRQ claims/WFI entries and prove
that disabling either UART IRQ mask or PLIC routing prevents echo/wakeup; restoring
routing wakes the target and drains the queued data. Mailboxes contain diagnostic
counters only, never console payload. This isolated backend unit test is not a
substitute for the real Linux UART1/xSPI acceptance test.

For a supplied Kotama ELF:

```sh
ERBIUM_ELF=/absolute/path/kotama.elf J=2 scripts/build-all.sh linux
scripts/run-uart-test.sh --kotama
```

This opens guest UART1 before loading, verifies the TamaGo banner/prompt, sends
`info\r` on that UART, and requires a **fresh** Erbium SoC/RAM response and returned
prompt. Seeing a banner in a development-host log or sending a command through a
separate host-side PTY is not this acceptance criterion. The earlier studied
Kotama ELF passed this full path during implementation.

Source-only focused tests: `scripts/test-uart-backend.sh`,
`tests/test_erbctl_console.py`, `tests/test_erbium_uart_client.py`, and
`tests/test_uart_runner.py`. Existing loader/reset and SmallVM transport tests
remain part of regression validation.
