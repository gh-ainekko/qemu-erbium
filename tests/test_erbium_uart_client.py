# SPDX-License-Identifier: GPL-2.0
"""Offline native PTY tests for the guest acceptance client (no ARM/QEMU needed).

The fake erbctl only announces release. The test's PTY master provides a serial
peer, independent of child stdout/stdin, to exercise raw mode and cleanup.
"""
import errno
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BANNER = b"ERBIUM-UART-SMOKE-v1\r\n"
BINARY = b"\x00\r\n\x11\x13\xff"
KOTAMA_BOOT = b"\r\ntamago/riscv64 (go1.27.1) \xe2\x80\xa2 Kotama\r\n"
KOTAMA_PROMPT = b"\x1b[31m> \x1b[0m"
KOTAMA_INFO = (b"SoC ..........: Erbium (eb680000) @ 200 MHz (rv64cfimsux)\r\n"
               b"Minions ......: 1\r\n"
               b"Runtime ......: go1.27.1 tamago/riscv64 thread 0\r\n"
               b"RAM ..........: 0x40000000-0x41000000 (16 MiB)\r\n")


class UartGuestClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name) / "uart-test"
        subprocess.run(
            ["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(cls.binary),
             str(ROOT / "linux/tools/erbium-uart-test.c")], check=True)
        cls.wrapper = Path(cls.directory.name) / "uart-test.sh"
        cls.wrapper.write_text(
            (ROOT / "linux/rootfs/erbium-uart-test.sh").read_text().replace(
                "/usr/bin/erbium-uart-test", str(cls.binary)))
        cls.wrapper.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def exercise(self, mode="echo", elf_override=None, wrapper=False, original_echo=False):
        kotama = mode.startswith("kotama")
        expected_elf = elf_override or (
            "/firmware/host-payload.elf" if kotama else "/firmware/uart-smoke.elf")
        with tempfile.TemporaryDirectory() as d:
            event = Path(d) / "releases"
            event.write_text("")
            fake = Path(d) / "erbctl"
            fake.write_text("""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
assert sys.argv[1:] == ["-d", "/dev/erbium0", "load", os.environ["TEST_ELF"],
                        "--mtd", "/dev/mtd0", "--verify", "--start"]
# A descriptor leaked to erbctl could steal the banner.
for p in Path("/proc/self/fd").iterdir():
    try:
        assert os.readlink(p) != os.environ["TEST_TTY"]
    except FileNotFoundError:
        pass
with open(os.environ["TEST_RELEASES"], "a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
    out.flush()
if os.environ["TEST_MODE"] == "loader-fail":
    sys.exit(7)
if os.environ["TEST_MODE"] == "loader-hang":
    time.sleep(10)
""")
            fake.chmod(0o755)
            master, slave = pty.openpty()
            tty_name = os.ttyname(slave)
            original = termios.tcgetattr(slave)
            # This PTY models a serial endpoint, not a login terminal. Early
            # rejection restores termios BEFORE the process exits; checking
            # process.poll() cannot prevent our next fragmented peer write
            # from racing that restore. Keep the endpoint's original echo off
            # so late SoC/banner bytes cannot be reflected back into "traffic".
            # Do not filter received bytes or weaken any response assertion.
            # A separate successful-echo case checks ECHO-on restoration when
            # the peer has no unsent output at the client's cleanup boundary.
            if original_echo:
                original[3] |= termios.ECHO
            else:
                original[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(slave, termios.TCSANOW, original)
            if mode == "pre-release-stale":
                os.write(master, b"\x00")
            os.set_blocking(master, False)
            env = dict(os.environ, TEST_TTY=tty_name, TEST_RELEASES=str(event),
                       TEST_MODE=mode, TEST_ELF=expected_elf)
            args = [str(self.wrapper if wrapper else self.binary),
                    "--tty", tty_name, "--erbctl", str(fake),
                    "--timeout-ms", "1000"]
            if elf_override:
                args += ["--elf", elf_override]  # Must survive subsequent --kotama.
            if kotama:
                args += ["--kotama"]
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env)
            traffic = bytearray()
            releases = 0
            pending = bytearray()
            signal_sent = False
            exclusive_seen = False
            response_sent = False
            deadline = time.monotonic() + 6
            try:
                while process.poll() is None and time.monotonic() < deadline:
                    count = len(event.read_text().splitlines())
                    if count > releases:
                        self.assertEqual(count, releases + 1)
                        releases = count
                        # Same TTY must still be exclusive at each CPU restart.
                        try:
                            extra = os.open(tty_name, os.O_RDWR | os.O_NOCTTY)
                        except OSError as e:
                            self.assertEqual(e.errno, errno.EBUSY)
                            exclusive_seen = True
                        else:
                            os.close(extra)
                            # CAP_SYS_ADMIN may bypass TIOCEXCL; flock remains.
                            self.assertEqual(os.getuid(), 0)
                        if mode == "echo":
                            pending += BANNER
                        elif mode == "wrong-banner":
                            pending += b"!" + BANNER
                        elif mode == "duplicate-banner":
                            pending += BANNER + BANNER
                        elif mode == "wrong-echo":
                            pending += BANNER
                        elif mode == "signal" and not signal_sent:
                            os.kill(process.pid, signal.SIGTERM)
                            signal_sent = True
                        elif mode == "kotama-signal":
                            os.kill(process.pid, signal.SIGTERM)
                        elif kotama:
                            pending += (b"\r\nnot-TamaGo\r\n" if mode == "kotama-no-banner"
                                        else KOTAMA_BOOT)
                            if mode == "kotama-boot-info-only":
                                pending += KOTAMA_INFO
                            pending += b"\x1b[36minfo\t # device information\r\n\x1b[0m\r\n"
                            if mode != "kotama-no-prompt":
                                pending += KOTAMA_PROMPT
                            if mode == "kotama-prequeued-info":
                                pending += KOTAMA_INFO + KOTAMA_PROMPT
                    r, w, _ = select.select(
                        [master], [master] if pending else [], [], 0.005)
                    if r:
                        try:
                            data = os.read(master, 4096)
                        except BlockingIOError:
                            data = b""
                        traffic += data
                        if mode == "echo":
                            pending += data
                        elif mode == "wrong-echo" and data:
                            pending += bytes([data[0] ^ 1]) + data[1:]
                        elif kotama and len(traffic) >= 5 and not response_sent:
                            self.assertEqual(traffic, b"info\r")
                            response_sent = True
                            pending += b"info\r\n"
                            if mode not in ("kotama-echo-only", "kotama-boot-info-only",
                                            "kotama-no-response"):
                                pending += (KOTAMA_INFO.replace(b"0x41000000", b"0x42000000")
                                            if mode == "kotama-wrong-ram" else KOTAMA_INFO)
                            if mode != "kotama-no-response":
                                pending += b"\r\n" + KOTAMA_PROMPT
                    if w:
                        # Fragment banners/echo to exercise partial reads.
                        n = os.write(master, pending[:7])
                        del pending[:n]
                stdout, stderr = process.communicate(timeout=2)
                self.assertEqual(termios.tcgetattr(slave), original,
                                 "client must restore all original termios")
                # Exclusion must be removed on success, failures and SIGTERM.
                fd = os.open(tty_name, os.O_RDWR | os.O_NOCTTY)
                os.close(fd)
                if os.getuid() != 0 and releases:
                    self.assertTrue(exclusive_seen)
                return process.returncode, stdout, stderr, releases, bytes(traffic)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(slave)
                os.close(master)

    def test_binary_burst_repeat_and_restore(self):
        rc, stdout, stderr, releases, traffic = self.exercise()
        self.assertEqual(rc, 0, stderr.decode())
        self.assertEqual(stdout, b"ALL UART TESTS PASSED\n")
        self.assertEqual(releases, 2)
        expected = b"".join(
            BINARY + bytes((i + round_number * 37) % 256 for i in range(1024))
            for round_number in range(2))
        self.assertEqual(traffic, expected)

    def test_original_echo_on_is_restored_after_success(self):
        rc, stdout, stderr, releases, traffic = self.exercise(original_echo=True)
        self.assertEqual(rc, 0, stderr.decode())
        self.assertEqual(stdout, b"ALL UART TESTS PASSED\n")
        self.assertEqual(releases, 2)
        self.assertEqual(traffic, b"".join(
            BINARY + bytes((i + round_number * 37) % 256 for i in range(1024))
            for round_number in range(2)))

    def test_wrong_banner_is_not_searched_past(self):
        rc, stdout, stderr, releases, _ = self.exercise("wrong-banner")
        self.assertNotEqual(rc, 0)
        self.assertEqual(stdout, b"")
        self.assertEqual(releases, 1)
        self.assertIn(b"banner offset 0", stderr)

    def test_duplicate_banner_is_stale_input(self):
        rc, _, stderr, releases, _ = self.exercise("duplicate-banner")
        self.assertNotEqual(rc, 0)
        self.assertEqual(releases, 1)
        self.assertIn(b"stale/extra byte", stderr)

    def test_stale_bytes_before_release_prevent_load(self):
        rc, _, stderr, releases, _ = self.exercise("pre-release-stale")
        self.assertNotEqual(rc, 0)
        self.assertEqual(releases, 0)
        self.assertIn(b"stale/extra byte 0x00", stderr)

    def test_wrong_echo_fails(self):
        rc, _, stderr, _, _ = self.exercise("wrong-echo")
        self.assertNotEqual(rc, 0)
        self.assertIn(b"echo mismatch", stderr)

    def test_missing_banner_has_wallclock_timeout(self):
        rc, _, stderr, _, _ = self.exercise("no-banner")
        self.assertNotEqual(rc, 0)
        self.assertIn(b"wall-clock timeout", stderr)

    def test_loader_failure(self):
        rc, _, stderr, _, _ = self.exercise("loader-fail")
        self.assertNotEqual(rc, 0)
        self.assertIn(b"erbctl load --verify --start failed", stderr)

    def test_loader_hang_has_wallclock_timeout(self):
        rc, _, stderr, _, _ = self.exercise("loader-hang")
        self.assertNotEqual(rc, 0)
        self.assertIn(b"wall-clock timeout", stderr)

    def test_signal_cleanup(self):
        rc, stdout, stderr, _, _ = self.exercise("signal")
        self.assertNotEqual(rc, 0)
        self.assertEqual(stdout, b"")
        self.assertIn(b"interrupted", stderr)

    def test_repeat_is_mandatory(self):
        r = subprocess.run([str(self.binary), "--rounds", "1"], capture_output=True)
        self.assertEqual(r.returncode, 2)

    def test_stdio_must_not_be_serial_peer(self):
        master, slave = pty.openpty()
        try:
            original = termios.tcgetattr(slave)
            r = subprocess.run([str(self.binary), "--tty", os.ttyname(slave)],
                               stdout=slave, stderr=subprocess.PIPE, timeout=2)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(b"must not be stdin/stdout/stderr", r.stderr)
            self.assertEqual(termios.tcgetattr(slave), original)
        finally:
            os.close(slave)
            os.close(master)

    def test_kotama_info_requires_real_response_and_one_guest_load(self):
        rc, stdout, stderr, releases, traffic = self.exercise("kotama")
        self.assertEqual(rc, 0, stderr.decode())
        self.assertEqual(stdout, b"ALL KOTAMA UART TESTS PASSED\n")
        self.assertEqual(releases, 1)
        self.assertEqual(traffic, b"info\r")

    def test_kotama_explicit_elf_override_survives_mode_selection(self):
        rc, _, stderr, _, traffic = self.exercise("kotama", "/firmware/custom.elf")
        self.assertEqual(rc, 0, stderr.decode())
        self.assertEqual(traffic, b"info\r")

    def test_wrapper_kotama_selects_real_payload_default(self):
        rc, stdout, stderr, releases, traffic = self.exercise("kotama", wrapper=True)
        self.assertEqual(rc, 0, stderr.decode())
        self.assertEqual(stdout, b"ALL KOTAMA UART TESTS PASSED\n")
        self.assertEqual(releases, 1)
        self.assertEqual(traffic, b"info\r")

    def test_wrapper_default_remains_hermetic(self):
        rc, stdout, stderr, releases, _ = self.exercise(wrapper=True)
        self.assertEqual(rc, 0, stderr.decode())
        self.assertEqual(stdout, b"ALL UART TESTS PASSED\n")
        self.assertEqual(releases, 2)

    def test_kotama_echo_alone_is_not_success(self):
        rc, stdout, stderr, _, traffic = self.exercise("kotama-echo-only")
        self.assertNotEqual(rc, 0)
        self.assertEqual(stdout, b"")
        self.assertEqual(traffic, b"info\r")
        self.assertIn(b"fresh info response missing SoC: Erbium", stderr)

    def test_kotama_boot_info_cannot_satisfy_command_response(self):
        rc, _, stderr, _, _ = self.exercise("kotama-boot-info-only")
        self.assertNotEqual(rc, 0)
        self.assertIn(b"fresh info response missing SoC: Erbium", stderr)

    def test_kotama_prequeued_info_fails_before_command(self):
        rc, _, stderr, _, traffic = self.exercise("kotama-prequeued-info")
        self.assertNotEqual(rc, 0)
        self.assertEqual(traffic, b"")
        self.assertIn(b"unexpected/stale text after initial Kotama prompt", stderr)

    def test_kotama_wrong_ram_fails(self):
        rc, _, stderr, _, _ = self.exercise("kotama-wrong-ram")
        self.assertNotEqual(rc, 0)
        self.assertIn(b"missing RAM: 0x40000000-0x41000000", stderr)

    def test_kotama_missing_boot_banner_fails(self):
        rc, _, stderr, _, traffic = self.exercise("kotama-no-banner")
        self.assertNotEqual(rc, 0)
        self.assertEqual(traffic, b"")
        self.assertIn(b"without tamago/riscv64 boot banner", stderr)

    def test_kotama_missing_prompt_times_out_before_command(self):
        rc, _, stderr, _, traffic = self.exercise("kotama-no-prompt")
        self.assertNotEqual(rc, 0)
        self.assertEqual(traffic, b"")
        self.assertIn(b"wall-clock timeout", stderr)

    def test_kotama_missing_response_times_out(self):
        rc, _, stderr, _, traffic = self.exercise("kotama-no-response")
        self.assertNotEqual(rc, 0)
        self.assertEqual(traffic, b"info\r")
        self.assertIn(b"wall-clock timeout", stderr)

    def test_kotama_signal_restores_terminal(self):
        rc, stdout, stderr, _, traffic = self.exercise("kotama-signal")
        self.assertNotEqual(rc, 0)
        self.assertEqual(stdout, b"")
        self.assertEqual(traffic, b"")
        self.assertIn(b"interrupted", stderr)

    def test_kotama_rejects_fixture_rounds_option(self):
        r = subprocess.run([str(self.binary), "--kotama", "--rounds", "2"],
                           capture_output=True)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
