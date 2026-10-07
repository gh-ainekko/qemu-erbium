# SPDX-License-Identifier: GPL-2.0
"""IRQ fixture unit proof, NOT a substitute for guest UART1/xSPI acceptance.

Always compile/inspect the tiny MRAM ELF. Opt in to the existing built backend:
UART_IRQ_TEST_EMU=$PWD/build/sw-sysemu/erbium_emu \
    python3 -m unittest discover -s tests -p test_uart_irq_fixture.py -v

No backend build, guest build, actual Kotama, or emulator -elf preload. Upload
PT_LOADs through the backend's existing CPU-memory test API, attach its serial
peer, verify sleeping/waking by assertion-only mailbox counters. All console
bytes go through the serial peer; MMIO reads never consume UART RX/TX.
"""
import os
from pathlib import Path
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]

BANNER = b"ERBIUM-UART-SMOKE-v1\r\n"
EMU = os.environ.get("UART_IRQ_TEST_EMU")


class UartIrqFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("riscv64-unknown-elf-gcc"):
            raise unittest.SkipTest("RISC-V fixture compiler not installed")
        cls.temp = tempfile.TemporaryDirectory()
        cls.elf = Path(cls.temp.name) / "uart-smoke.elf"
        obj = Path(cls.temp.name) / "uart-smoke.o"
        subprocess.run(
            ["riscv64-unknown-elf-gcc", "-c", "-nostdlib", "-march=rv64imc_zicsr",
             "-mabi=lp64", "-mcmodel=medany", "-mno-relax", "-o", str(obj),
             str(ROOT / "linux/firmware/uart-smoke.S")], check=True)
        subprocess.run(
            ["riscv64-unknown-elf-ld", "-m", "elf64lriscv", "--no-relax", "-T",
             str(ROOT / "linux/firmware/uart-smoke.ld"), "-o", str(cls.elf), str(obj)],
            check=True)
        cls.image = cls.elf.read_bytes()
        cls.entry, phoff = struct.unpack_from("<QQ", cls.image, 24)
        phsize, phnum = struct.unpack_from("<HH", cls.image, 54)
        cls.loads = []
        for i in range(phnum):
            values = struct.unpack_from("<IIQQQQQQ", cls.image, phoff + i * phsize)
            kind, flags, offset, va, pa, filesz, memsz, align = values
            if kind == 1:
                cls.loads.append((flags, offset, va, pa, filesz, memsz, align))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_mram_elf_entry_isa_and_small_footprint(self):
        self.assertEqual(self.image[:7], b"\x7fELF\x02\x01\x01")
        self.assertEqual(self.entry, 0x40010000)
        self.assertEqual(len(self.loads), 2)
        for _, offset, va, pa, filesz, memsz, align in self.loads:
            self.assertEqual(va, pa)
            self.assertGreaterEqual(pa, 0x40000000)
            self.assertLessEqual(pa + memsz, 0x41000000)
            self.assertLessEqual(filesz, memsz)
            self.assertLessEqual(offset + filesz, len(self.image))
            self.assertEqual(pa % align, offset % align)
        self.assertLessEqual(sum(v[5] for v in self.loads), 1024)
        attrs = subprocess.check_output(["riscv64-unknown-elf-readelf", "-A", str(self.elf)])
        self.assertIn(b"zicsr", attrs)
        flags, offset, _, _, size, _, _ = self.loads[0]
        self.assertEqual(flags, 5)
        text = self.image[offset:offset + size]
        self.assertEqual(text.count(BANNER + b"\0"), 1)
        self.assertIn(struct.pack("<I", 0x10500073), text)  # WFI
        self.assertIn(struct.pack("<I", 0x30200073), text)  # MRET

    @unittest.skipUnless(EMU, "set UART_IRQ_TEST_EMU to test an already-built backend")
    def test_real_irq_wfi_masking_burst_and_reload(self):
        sys.path.insert(0, str(ROOT / "et-platform/sw-sysemu/tools"))
        from erb_client import ErbClient

        def u32(addr):
            status, data = control.read(addr, 4)
            self.assertEqual(status, 0, f"read {addr:#x}")
            return struct.unpack("<I", data)[0]

        def write(addr, data):
            self.assertEqual(control.write(addr, data), 0, f"write {addr:#x}")

        def w32(addr, value):
            write(addr, struct.pack("<I", value))

        def recv_exact(expected):
            deadline = time.monotonic() + 5
            data = bytearray()
            while len(data) < len(expected):
                peer.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = peer.recv(len(expected) - len(data))
                self.assertTrue(chunk, "serial peer disconnected")
                data += chunk
            self.assertEqual(bytes(data), expected)

        def sleeping(min_irq, min_wfi):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                irq, wfi = u32(0x02000068), u32(0x02000070)
                self.assertLess(wfi, 0xbad00000, "unexpected firmware trap")
                if irq >= min_irq and wfi >= min_wfi:
                    time.sleep(0.05)
                    self.assertEqual(u32(0x02000068), irq, "idle IRQ storm")
                    self.assertEqual(u32(0x02000070), wfi, "main is polling, not asleep")
                    self.assertFalse(select.select([peer], [], [], 0)[0], "extra serial bytes")
                    return irq, wfi
                time.sleep(0.01)
            self.fail(f"did not return to WFI: irq={irq}, wfi={wfi}")

        def duplex(payload):
            peer.setblocking(False)
            sent, received = 0, bytearray()
            deadline = time.monotonic() + 5
            try:
                while sent < len(payload) or len(received) < len(payload):
                    self.assertLess(time.monotonic(), deadline, "IRQ echo burst timed out")
                    r, w, _ = select.select(
                        [peer], [peer] if sent < len(payload) else [], [], 0.05)
                    if r:
                        chunk = peer.recv(4096)
                        self.assertTrue(chunk, "serial peer disconnected")
                        received += chunk
                    if w:
                        sent += peer.send(payload[sent:])
                self.assertEqual(bytes(received), payload)
            finally:
                peer.settimeout(5)

        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            api, serial = p / "api.sock", p / "uart.sock"
            control = peer = None
            with (p / "backend.log").open("wb") as log:
                process = subprocess.Popen(
                    [str(Path(EMU).resolve()), "-single_thread", "-minions", "0x1",
                     "--start-held", "--api-socket", str(api), "--uart-socket", str(serial),
                     "--mram-file", str(p / "empty-mram.img")],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 5
                    while not api.exists() or not serial.exists():
                        self.assertIsNone(process.poll(), (p / "backend.log").read_text())
                        self.assertLess(time.monotonic(), deadline, "backend sockets timed out")
                        time.sleep(0.01)
                    control = ErbClient(str(api))
                    control.sock.settimeout(5)
                    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    peer.connect(str(serial))  # Before CPU release; same peer both runs.
                    peer.settimeout(5)
                    for round_number in range(2):
                        w32(0x02000028, 6)
                        for _, offset, _, pa, filesz, memsz, _ in self.loads:
                            write(pa, self.image[offset:offset + filesz] +
                                  bytes(memsz - filesz))
                        write(0x80d00018, struct.pack("<Q", self.entry))
                        write(0x80f40240, struct.pack("<Q", 0xfe))
                        write(0x80f40010, struct.pack("<Q", 0xff))
                        w32(0x02000028, 4)
                        recv_exact(BANNER)
                        irq, wfi = sleeping(0, 1)
                        self.assertEqual(irq, 0, "no RX interrupt before peer sends")
                        self.assertEqual(u32(0x02004050) & 0x3ff, 4, "RX-only UART mask")
                        self.assertEqual(u32(0xa0002000), 8, "PLIC source3 context0")

                        # Negative proof: RX pending while mask=0 MUST NOT echo
                        # or wake. Reenable only RX bit2, then the ISR must echo.
                        w32(0x02004050, 0)
                        peer.sendall(b"\x00")
                        self.assertFalse(select.select([peer], [], [], 0.15)[0])
                        self.assertEqual(u32(0x02000068), irq)
                        self.assertEqual(u32(0x02000070), wfi)
                        self.assertTrue(u32(0x02004048) & 4)
                        w32(0x02004050, 4)
                        recv_exact(b"\x00")
                        irq, wfi = sleeping(irq + 1, wfi + 1)

                        # Independent PLIC gate: locally buffered UART bytes
                        # cannot be echoed by a polling main-loop fallback.
                        w32(0xa0002000, 0)
                        peer.sendall(b"\xff")
                        self.assertFalse(select.select([peer], [], [], 0.15)[0])
                        self.assertEqual(u32(0x02000068), irq)
                        self.assertEqual(u32(0x02000070), wfi)
                        w32(0xa0002000, 8)
                        recv_exact(b"\xff")
                        irq, wfi = sleeping(irq + 1, wfi + 1)

                        payload = b"\0\r\n\x11\x13\xff" + bytes(
                            (i + round_number * 37) % 256 for i in range(1024))
                        duplex(payload)
                        irq, wfi = sleeping(irq + 1, wfi + 1)
                        # Sticky STATUS threshold is cleared separately from
                        # RAW; only RX-not-empty is enabled throughout.
                        self.assertFalse(u32(0x02004018) & (1 << 8))
                        self.assertFalse(u32(0x02004048) & ((1 << 8) | 4))
                        self.assertEqual(u32(0x02004050) & 0x3ff, 4)
                        print(f"IRQ fixture round {round_number + 1}: "
                              f"UART claims={irq}, WFI entries={wfi}, exact burst PASS")
                except BaseException:
                    if control is not None:
                        print(f"firmware diagnostics: IRQ={u32(0x02000068):#x}, "
                              f"WFI/trap={u32(0x02000070):#x}", file=sys.stderr)
                    print((p / "backend.log").read_text(), file=sys.stderr)
                    raise
                finally:
                    if control is not None:
                        control.close()
                    if peer is not None:
                        peer.close()
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    if process.returncode not in (0, -15):
                        print((p / "backend.log").read_text(), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
