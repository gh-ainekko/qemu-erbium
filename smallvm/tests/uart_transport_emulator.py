#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Bounded >1024-byte UART echo regression using an actual Erbium MMIO firmware."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import time
import tty


def read_available(fd):
    try:
        return os.read(fd, 65536)
    except OSError as error:
        if error.errno in (errno.EAGAIN, errno.EIO):
            return b""
        raise


def run(emu, elf, output, timeout):
    output.mkdir(parents=True, exist_ok=True)
    blob = elf.read_bytes()
    assert blob[:6] == b"\x7fELF\x02\x01"
    entry = struct.unpack_from("<Q", blob, 24)[0]
    master, slave = pty.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    command = [str(emu), "-elf_load", str(elf), "-reset_pc", hex(entry),
               "-single_thread", "-minions", "0x1", "-max_cycles", "-1",
               "-uart_rx_file", os.ttyname(slave), "-uart_tx_file", os.ttyname(slave)]
    proc = None
    received = bytearray()
    started = time.monotonic()
    deadline = started + timeout
    result = {"status": "FAIL", "command": command,
              "elf_sha256": hashlib.sha256(blob).hexdigest(),
              "emulator_sha256": hashlib.sha256(emu.read_bytes()).hexdigest()}
    try:
        with (output / "emulator.log").open("wb") as log:
            proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            ready = bytearray()
            while ready != b"READY":
                assert time.monotonic() < deadline, "firmware readiness timeout"
                assert proc.poll() is None, f"emulator exited: {proc.returncode}"
                select.select([master], [], [], 0.01)
                ready.extend(read_available(master))
                assert b"READY".startswith(ready), f"unexpected readiness bytes: {ready!r}"
            # Each value occurs, including NUL, CR/LF, XON/XOFF, 0xFA/FB and FF.
            # Send one unpaced 4096-byte upload while the emulator is stopped,
            # then a much larger continuous stream under deliberate TX pressure.
            burst = bytes((i * 73 + i // 256) & 255 for i in range(4096))
            proc.send_signal(signal.SIGSTOP)
            try:
                # Wait for the stop rather than racing a scheduler timeslice.
                os.waitpid(proc.pid, os.WUNTRACED)
                sent = os.write(master, burst)
                assert sent == len(burst), f"PTY could not queue initial burst: {sent}"
            finally:
                proc.send_signal(signal.SIGCONT)
            while len(received) < len(burst):
                assert time.monotonic() < deadline, "4096-byte echo timeout"
                assert proc.poll() is None, f"emulator exited: {proc.returncode}"
                select.select([master], [], [], 0.01)
                received.extend(read_available(master))
            assert received == burst, "initial burst corruption"
            bulk = bytes((i * 157 + i // 251) & 255 for i in range(131072))
            pos = 0
            echoed = bytearray()
            blocked = False
            # Do not read TX for at least one second. Both kernel PTY queues
            # and the guest TX FIFO must fill without stopping the emulator.
            pressure_end = time.monotonic() + 1
            while time.monotonic() < pressure_end:
                assert time.monotonic() < deadline, "TX pressure timeout"
                assert proc.poll() is None, f"emulator exited: {proc.returncode}"
                if pos < len(bulk):
                    try:
                        pos += os.write(master, bulk[pos:pos+8192])
                    except BlockingIOError:
                        blocked = True
                time.sleep(0.005)
            assert blocked and pos < len(bulk), "did not establish end-to-end backpressure"
            while pos < len(bulk) or len(echoed) < len(bulk):
                assert time.monotonic() < deadline, (
                    f"bulk echo timeout: sent {pos}, received {len(echoed)}")
                assert proc.poll() is None, f"emulator exited: {proc.returncode}"
                reads, writes, _ = select.select([master], [master] if pos < len(bulk) else [], [], 0.01)
                if reads:
                    echoed.extend(read_available(master))
                if writes:
                    try:
                        pos += os.write(master, bulk[pos:pos+8192])
                    except BlockingIOError:
                        pass
            assert echoed == bulk, "binary bulk corruption after host backpressure"
            received.extend(echoed)
            # Require an idle empty transport and a subsequent small upload.
            select.select([master], [], [], 0.1)
            assert not read_available(master), "unexpected duplicated trailing bytes"
            tail = b"\x00\xff\x11\x13\xfa\xfb\r\n"
            assert os.write(master, tail) == len(tail)
            tail_echo = bytearray()
            while len(tail_echo) < len(tail):
                assert time.monotonic() < deadline, "post-burst echo timeout"
                select.select([master], [], [], 0.01)
                tail_echo.extend(read_available(master))
            assert tail_echo == tail
            received.extend(tail_echo)
            result.update(status="PASS", initial_burst_bytes=len(burst),
                          backpressure_bytes=len(bulk), host_eagain=blocked,
                          echoed_bytes=len(received))
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        os.close(master)
        os.close(slave)
        (output / "echo.bin").write_bytes(received)
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument("--emu", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    run(args.emu.resolve(), args.elf.resolve(), args.output.resolve(), args.timeout)


if __name__ == "__main__":
    main()
