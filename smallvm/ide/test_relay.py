#!/usr/bin/python3
"""Exercise the production PTY wire, all byte values + bounded backpressure."""
import os
from pathlib import Path
import tempfile
import time
from supervisor import LIMIT, Session


def main():
    with tempfile.TemporaryDirectory(prefix="smallvm-ide-relay-") as temp:
        root = Path(temp)
        session = Session(root, root / "run", root / "logs")
        payloads = [
            bytes(range(256)) * 10000,
            bytes(reversed(range(256))) * 9000,
        ]
        received = [bytearray(), bytearray()]
        offsets = [0, 0]
        for fd in session.slaves:
            os.set_blocking(fd, False)
        try:
            started = time.monotonic()
            saturated = False
            while any(len(received[i]) < len(payloads[1-i]) for i in (0, 1)):
                assert time.monotonic() - started < 20, "relay stalled"
                for i, slave in enumerate(session.slaves):
                    try:
                        part = payloads[i][offsets[i]:offsets[i] + 16384]
                        if part:
                            offsets[i] += os.write(slave, part)
                    except BlockingIOError:
                        pass
                session.relay(0)
                assert all(len(queue) <= LIMIT for queue in session.pending)
                # Pause readers until BOTH directions saturate their exact
                # bound; continue pumping while saturated to detect drops.
                if not saturated:
                    saturated = all(len(queue) == LIMIT for queue in session.pending)
                if saturated:
                    for i, slave in enumerate(session.slaves):
                        try:
                            received[i].extend(os.read(slave, 32768))
                        except BlockingIOError:
                            pass
            assert saturated, "test never exercised bounded backpressure"
            for i in (0, 1):
                assert received[1-i] == payloads[i], f"direction {i}: byte loss/change"
                session.uart_logs[i].flush()
                assert Path(session.uart_logs[i].name).read_bytes() == payloads[i]
            assert session.command("arbitrary-shell-command")["error"]
            print("PASS: opposite raw PTYs; all bytes; 4.86 MB duplex; 1 MiB "
                  "backpressure; partial writes; exact binary transcripts")
        finally:
            session.close()
        assert not (root / "run/erbium-serial").exists()
        assert not (root / "run/control.sock").exists()
        print("PASS: owned PTYs, symlinks and control socket cleaned up")


if __name__ == "__main__":
    main()
