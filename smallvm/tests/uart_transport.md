# Shakti byte-stream transport regression

Run from the repository root:

```sh
# Native device harness with ASan/UBSan (needs native C++, no cross toolchain).
bash smallvm/tests/uart_transport.sh

# Build the emulator using the normal repository build.
J=2 scripts/build-all.sh sysemu

# Build a freestanding UART echo firmware and run on the actual emulator.
bash smallvm/tests/uart_transport.sh --emulator

# Existing SmallVM and general socket/reset regressions:
python3 smallvm/tests/run_emulator.py build/smallvm/smallvm.elf \
  --mode protocol --output build/smallvm/uart-transport/protocol
python3 smallvm/tests/run_emulator.py build/smallvm/smallvm-selftest.elf \
  --mode selftest --output build/smallvm/uart-transport/selftest
python3 et-platform/sw-sysemu/tests/erbium/host/test_cpu_reset.py \
  --emu "$PWD/dist/bin/erbium_emu"
```

`UART_TRANSPORT_BUILD` overrides the output directory and `ERBIUM_EMU` overrides
the emulator used by `--emulator`. Each process is bounded and cleaned up; no
bridge or persistent service is started.

## Coverage

The native harness includes the **actual** `shakti_uart.h`, with only the
System enable/PLIC boundary stubbed. The real file descriptors exercise kernel
backpressure and reconnect behavior; link-time read/write wrappers add
deterministic EINTR/EAGAIN, zero-write, and partial-write cases.

- 4,096 binary bytes in one host write, a 16-byte guest RX FIFO, and 100 polls
  while full that perform **no additional host reads** or overruns.
- Empty blocking descriptors become nonblocking before stream I/O.
- TX retains data/fullness under real pipe EAGAIN and resumes in order.
- Partial writes and ring wrap; binary NUL/FF/XON/XOFF and framing-marker bytes.
- Named FIFO EOF/writer reconnect, PTY EIO/slave-peer reconnect, descriptors
  above `FD_SETSIZE`, and explicit permanent-error reporting.
- Pipe disconnect cannot kill standalone sysemu with SIGPIPE; masking is
  per-thread, preserves an already-pending SIGPIPE, and restores the old mask.

The emulator test runs real RISC-V polling firmware with 32-bit UART MMIO;
it does not synthesize MicroBlocks responses. It queues one 4,096-byte upload
while the emulator is stopped, then verifies exact echo. Next, it sends
131,072 binary bytes while deliberately not reading the host TX endpoint for
one second, establishes host EAGAIN, resumes, and checks exact ordered echo.
It rejects duplicated trailing bytes and checks a final short transfer.
This is a transport regression, **not** full MicroBlocks IDE interoperability.

## Attachment contract

Host fds model reliable flow-controlled **streams**, not physical baud-clocked
UART input. Fullness leaves bytes in the host queue; parity/framing/physical
overruns are not modeled. The hardware RX/TX FIFOs remain bounded to 16 bytes.
Guest writes while TX_FULL are still outside the guest driver's contract.

The device borrows descriptors and adds `O_NONBLOCK` before stream I/O,
affecting duplicates of the same open-file description. The owner must not
clear that flag while attached. Regular disk files still have ordinary host
filesystem latency: `O_NONBLOCK` cannot make disk I/O asynchronous.

EOF/EAGAIN/EIO/EPIPE retain fd ownership and queued bytes, allowing named FIFO
and PTY slave peers to reconnect. Queued output is intentionally **not**
discarded on disconnect, so a disconnected guest can stall on TX_FULL.
A closed PTY master cannot resurrect that PTY pair: retain the bridge's
master or let the fd owner attach a new pair. Detach with `-1` and poll before
closing/reusing an fd number; reattachment preserves queued bytes.

The CLI's Shakti RX file open already uses `O_NONBLOCK`. Its TX file open
currently does not: the device makes it nonblocking before the first write,
which handles the raw-PTY IDE bridge. A named TX FIFO still requires a reader
at **initial CLI open**, before attachment. Changing that CLI open to
`O_NONBLOCK` (and defining ENXIO startup/retry behavior) belongs to setup,
not this device-only change; no other UART setup or backend paths are changed.
