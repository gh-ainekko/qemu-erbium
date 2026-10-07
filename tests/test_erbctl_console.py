"""Offline native tests of the real direct-UART console.

Run: python3 -m unittest discover -s tests -p 'test_erbctl_console.py' -v
Python stdlib, native gcc, and Linux PTYs only. No backend/kernel build, root,
hardware, network, or external Python packages. Retained descriptors let the
parent verify restoration of shared open-file-description flags and termios.
"""
import errno
import fcntl
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import tempfile
import termios
import threading
import time
import tty
import unittest

ROOT = Path(__file__).resolve().parents[1]
_build = None
NATIVE = HARNESS = None


def setUpModule():
    global _build, NATIVE, HARNESS
    if not shutil.which("gcc"):
        raise unittest.SkipTest("native gcc required")
    _build = tempfile.TemporaryDirectory(prefix="erbctl-console-build-")
    NATIVE = Path(_build.name) / "erbctl"
    HARNESS = Path(_build.name) / "console-harness"
    # A tiny same-process caller checks signal dispositions/mask after returning;
    # all UART/terminal work remains in the production console implementation.
    harness = Path(_build.name) / "harness.c"
    harness.write_text(r"""
#include <signal.h>
#include <stdio.h>
#include "erbium-console.h"
static void previous(int sig) { (void)sig; }
int main(int argc, char **argv)
{
    const int signals[] = {SIGINT, SIGTERM, SIGHUP, SIGPIPE};
    struct sigaction action = {0}, after;
    sigset_t mask, after_mask;
    sigemptyset(&action.sa_mask);
    action.sa_handler = previous;
    for (int i = 0; i < 4; i++)
        if (sigaction(signals[i], &action, NULL)) return 90;
    sigemptyset(&mask);
    sigaddset(&mask, SIGUSR1);
    if (sigprocmask(SIG_SETMASK, &mask, NULL)) return 91;
    int result = erbctl_console(argc - 1, argv + 1);
    for (int i = 0; i < 4; i++) {
        if (sigaction(signals[i], NULL, &after) ||
            after.sa_handler != previous) return 92;
    }
    if (sigprocmask(SIG_SETMASK, NULL, &after_mask)) return 93;
    for (int i = 1; i < NSIG; i++)
        if (sigismember(&mask, i) != sigismember(&after_mask, i)) return 94;
    fprintf(stderr, "HARNESS restored signals\n");
    return result;
}
""")
    common = ["gcc", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
              "-I", str(ROOT / "linux/tools")]
    try:
        for target, sources in (
            (NATIVE, [ROOT / "linux/tools/erbctl.c",
                      ROOT / "linux/tools/erbium-loader.c",
                      ROOT / "linux/tools/erbium-console.c"]),
            (HARNESS, [harness, ROOT / "linux/tools/erbium-console.c"]),
        ):
            subprocess.run(common + [str(p) for p in sources] + ["-o", str(target)],
                           check=True, capture_output=True, text=True, timeout=30)
    except BaseException:
        _build.cleanup()
        raise


def tearDownModule():
    if _build:
        _build.cleanup()


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.fds = set()
        self.children = []
        self.addCleanup(self.cleanup)
        self.master, self.slave = self.keep(os.openpty())
        self.path = os.ttyname(self.slave)
        self.original_uart = termios.tcgetattr(self.slave)
        self.input_r, self.input_w = self.keep(os.pipe())
        self.output_r, self.output_w = self.keep(os.pipe())
        self.input_flags = fcntl.fcntl(self.input_r, fcntl.F_GETFL)
        self.output_flags = fcntl.fcntl(self.output_w, fcntl.F_GETFL)
        self.local = None
        self.original_local = None
        self.ready_text = b""

    def keep(self, fds):
        self.fds.update(fds)
        return fds

    def close(self, fd):
        if fd in self.fds:
            self.fds.remove(fd)
            os.close(fd)

    def cleanup(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            if child.stderr:
                child.stderr.close()
        for fd in self.fds:
            os.close(fd)

    def start(self, *, interactive=False, output=None, input_fd=None, args=(),
              harness=False, ready=True):
        if interactive:
            self.local_master, self.local = self.keep(os.openpty())
            self.original_local = termios.tcgetattr(self.local)
            input_fd = self.local
            self.input_flags = fcntl.fcntl(input_fd, fcntl.F_GETFL)
        if input_fd is None:
            input_fd = self.input_r
        argv = ([str(HARNESS)] if harness else
                [str(NATIVE), "-d", "/nonexistent-control-no-console-open", "console"])
        child = subprocess.Popen(
            [*argv, self.path, *args], stdin=input_fd,
            stdout=self.output_w if output is None else output, stderr=subprocess.PIPE)
        self.children.append(child)
        if ready:
            text = bytearray()
            deadline = time.monotonic() + 3
            while b"Ctrl-]" not in text:
                left = deadline - time.monotonic()
                self.assertGreater(left, 0, f"console not ready: {text!r}")
                if select.select([child.stderr], [], [], left)[0]:
                    block = os.read(child.stderr.fileno(), 4096)
                    self.assertTrue(block, f"console exited during setup: {text!r}")
                    text.extend(block)
            self.ready_text = bytes(text)
        return child

    def read_exact(self, fd, count, timeout=5):
        data = bytearray()
        deadline = time.monotonic() + timeout
        while len(data) < count:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], max(0, left))[0]:
                raise AssertionError(f"read timed out: {len(data)}/{count} bytes")
            block = os.read(fd, min(65536, count - len(data)))
            if not block:
                raise AssertionError(f"unexpected EOF at {len(data)}/{count}")
            data.extend(block)
        return bytes(data)

    def send(self, fd, data, timeout=8):
        # This affects only the parent's writer/master description, not the
        # child's stdin reader or UART slave open-file description.
        os.set_blocking(fd, False)
        deadline = time.monotonic() + timeout
        while data:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [fd], [], max(0, left))[1]:
                raise AssertionError("write timed out")
            try:
                count = os.write(fd, data[:65536])
            except BlockingIOError:
                continue
            data = data[count:]

    def finish(self, child, *, code=0, timeout=5):
        child.wait(timeout=timeout)
        text = self.ready_text + child.stderr.read()
        self.assertEqual(child.returncode, code, text.decode(errors="replace"))
        return text

    def restored(self):
        self.assertEqual(termios.tcgetattr(self.slave), self.original_uart)
        input_fd = self.local if self.local is not None else self.input_r
        self.assertEqual(fcntl.fcntl(input_fd, fcntl.F_GETFL), self.input_flags)
        self.assertEqual(fcntl.fcntl(self.output_w, fcntl.F_GETFL), self.output_flags)
        if self.local is not None:
            self.assertEqual(termios.tcgetattr(self.local), self.original_local)
        reopened = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(reopened, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(reopened)

    def test_bidirectional_all_bytes_pipe_and_default_baud(self):
        child = self.start()
        payload = bytes(range(256)) * 4
        raw = termios.tcgetattr(self.slave)
        self.assertEqual(raw[4:6], [termios.B115200, termios.B115200])
        self.assertEqual(raw[2] & termios.CSIZE, termios.CS8)
        self.assertEqual(raw[2] & (termios.PARENB | termios.CSTOPB | termios.CRTSCTS), 0)
        self.assertEqual(raw[2] & (termios.CLOCAL | termios.CREAD),
                         termios.CLOCAL | termios.CREAD)
        self.assertEqual(raw[0] & (termios.IXON | termios.IXOFF | termios.IXANY), 0)
        self.send(self.input_w, payload)
        self.assertEqual(self.read_exact(self.master, len(payload)), payload)
        self.send(self.master, payload[::-1])
        self.assertEqual(self.read_exact(self.output_r, len(payload)), payload[::-1])
        self.close(self.input_w)
        text = self.finish(child)
        self.assertNotIn(b"nonexistent-control", text)
        self.assertIn(b"Ctrl-]", text)
        self.assertFalse(select.select([self.output_r], [], [], 0)[0])
        self.restored()

    def test_interactive_escape_and_restoration(self):
        child = self.start(interactive=True, args=("--baud", "9600"))
        self.assertFalse(termios.tcgetattr(self.local)[3] & (termios.ICANON | termios.ECHO))
        self.assertEqual(termios.tcgetattr(self.slave)[4:6], [termios.B9600] * 2)
        payload = b"\x00\xff\r\n\x03\x04\x11\x13\x7f"
        self.send(self.local_master, payload)
        self.assertEqual(self.read_exact(self.master, len(payload)), payload)
        self.send(self.master, b"guest\r\n")
        self.assertEqual(self.read_exact(self.output_r, 7), b"guest\r\n")
        self.send(self.local_master, b"\x1d")
        self.finish(child)
        self.restored()

    def test_signals_restore_termios_flags_and_dispositions(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                child = self.start(interactive=True, harness=True)
                child.send_signal(sig)
                text = self.finish(child, code=128 + sig)
                self.assertIn(b"HARNESS restored signals", text)
                self.restored()

    def test_escape_restores_signal_dispositions(self):
        child = self.start(interactive=True, harness=True)
        self.send(self.local_master, b"\x1d")
        self.assertIn(b"HARNESS restored signals", self.finish(child))
        self.restored()

    def test_no_flush_of_already_buffered_uart_output(self):
        tty.setraw(self.slave)
        self.original_uart = termios.tcgetattr(self.slave)
        payload = b"boot-before-attach\x00\xff\r\n"
        self.send(self.master, payload)
        child = self.start()
        self.assertEqual(self.read_exact(self.output_r, len(payload)), payload)
        self.close(self.input_w)
        self.finish(child)
        self.restored()

    def test_pipe_eof_waits_for_received_tail_without_busy_loop(self):
        child = self.start()
        start = time.monotonic()
        self.close(self.input_w)
        time.sleep(0.15)
        # EOF should remove stdin from poll rather than spin on its POLLHUP.
        cpu = Path(f"/proc/{child.pid}/stat").read_text().split()
        ticks = int(cpu[13]) + int(cpu[14])
        self.assertLess(ticks / os.sysconf("SC_CLK_TCK"), 0.1)
        self.send(self.master, b"late-reply\x00\xff")
        self.assertEqual(self.read_exact(self.output_r, 12), b"late-reply\x00\xff")
        self.finish(child)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.6)
        self.assertLess(elapsed, 2)
        self.restored()

    def test_sparse_replies_near_eof_deadline_report_incomplete_quiet_period(self):
        child = self.start()
        self.close(self.input_w)
        start = time.monotonic()
        # Keep the peer active without leaving bytes in the local queues.
        # The final byte at 2.75 s needs until 3.25 s to establish quiet,
        # beyond the advertised 3 s hard drain deadline.
        for i in range(19):
            time.sleep(max(0, start + 0.05 + i * 0.15 - time.monotonic()))
            self.send(self.master, bytes((i,)))
            self.assertEqual(self.read_exact(self.output_r, 1), bytes((i,)))
        last_reply = time.monotonic()
        text = self.finish(child, code=1, timeout=2)
        self.assertIn(b"drain timed out", text)
        self.assertLess(time.monotonic() - last_reply, 0.5)
        self.assertLess(time.monotonic() - start, 3.5)
        self.restored()

    def test_pipe_eof_drains_queued_transmit_before_reply_grace(self):
        child = self.start()
        payload = bytes(range(256)) * 128
        errors = []
        def writer():
            try:
                self.send(self.input_w, payload)
                self.close(self.input_w)
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=writer)
        thread.start()
        time.sleep(0.2)
        self.assertEqual(self.read_exact(self.master, len(payload)), payload)
        self.send(self.master, b"last reply")
        self.assertEqual(self.read_exact(self.output_r, 10), b"last reply")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.finish(child)
        self.restored()

    def test_eof_grace_probes_uart_after_stdout_backpressure(self):
        capacity = fcntl.fcntl(self.output_w, fcntl.F_GETPIPE_SZ)
        if capacity < 16384:
            self.skipTest("needs a pipe large enough for a whole console RX queue")
        padding = b"p" * capacity
        self.assertEqual(os.write(self.output_w, padding), capacity)
        child = self.start()
        payload = bytes(range(256)) * 128
        errors = []
        def writer():
            try:
                self.send(self.master, payload)
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=writer)
        thread.start()
        self.close(self.input_w)
        time.sleep(0.7)  # quiet deadline passes while stdout's pipe is full
        self.assertEqual(self.read_exact(self.output_r, capacity), padding)
        self.assertEqual(self.read_exact(self.output_r, len(payload)), payload)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.finish(child)
        self.restored()

    def test_exclusive_uart_and_cooperative_lock(self):
        child = self.start()
        with self.assertRaises(OSError) as context:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            os.close(fd)
        self.assertEqual(context.exception.errno, errno.EBUSY)
        with self.assertRaises(BlockingIOError):
            fcntl.flock(self.slave, fcntl.LOCK_EX | fcntl.LOCK_NB)
        contender = self.start(ready=False)
        self.finish(contender, code=1)
        self.assertFalse(termios.tcgetattr(self.slave)[3] & termios.ICANON)
        child.send_signal(signal.SIGTERM)
        self.finish(child, code=128 + signal.SIGTERM)
        self.restored()

    def test_external_cooperative_lock_prevents_termios_change(self):
        fcntl.flock(self.slave, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = self.start(ready=False, harness=True)
        text = self.finish(child, code=1)
        self.assertIn(b"cooperative lock", text)
        self.assertIn(b"HARNESS restored signals", text)
        self.assertEqual(termios.tcgetattr(self.slave), self.original_uart)
        fcntl.flock(self.slave, fcntl.LOCK_UN)
        self.restored()

    def test_broken_stdout_restores_everything_not_sigpipe_death(self):
        self.close(self.output_r)
        child = self.start(interactive=True, harness=True)
        self.send(self.master, b"cannot write output")
        text = self.finish(child, code=1)
        self.assertIn(b"stdout write", text)
        self.assertIn(b"HARNESS restored signals", text)
        self.restored()

    def test_readonly_stdout_error_restores_everything(self):
        read_only = os.open("/dev/null", os.O_RDONLY)
        self.keep((read_only,))
        original = fcntl.fcntl(read_only, fcntl.F_GETFL)
        child = self.start(interactive=True, output=read_only, harness=True)
        self.send(self.master, b"output")
        text = self.finish(child, code=1)
        self.assertIn(b"stdout write", text)
        self.assertIn(b"HARNESS restored signals", text)
        self.assertEqual(fcntl.fcntl(read_only, fcntl.F_GETFL), original)
        self.restored()

    def test_readonly_uart_is_reported_before_local_terminal_changes(self):
        if os.geteuid() == 0:
            self.skipTest("root bypasses UART permissions")
        mode = os.stat(self.path).st_mode & 0o777
        try:
            os.chmod(self.path, 0o400)
            child = self.start(interactive=True, ready=False)
            text = self.finish(child, code=1)
            self.assertIn(b"Permission denied", text)
        finally:
            os.chmod(self.path, mode)
        self.restored()

    def test_uart_hangup_bounded_and_local_restored(self):
        child = self.start(interactive=True)
        # Confirm bytes received before hangup are forwarded, then disconnect.
        self.send(self.master, b"tail before hangup")
        self.assertEqual(self.read_exact(self.output_r, 18), b"tail before hangup")
        start = time.monotonic()
        self.close(self.master)
        text = self.finish(child, code=1, timeout=4)
        self.assertIn(b"UART disconnected", text)
        self.assertLess(time.monotonic() - start, 3.5)
        self.assertEqual(termios.tcgetattr(self.local), self.original_local)
        self.assertEqual(fcntl.fcntl(self.local, fcntl.F_GETFL), self.input_flags)
        self.assertEqual(fcntl.fcntl(self.output_w, fcntl.F_GETFL), self.output_flags)
        # A dead PTY returns EIO for all termios/exclusive ioctls; cleanup
        # reports the failed restoration but still restores local descriptors.
        self.assertIn(b"restore UART termios", text)

    def test_large_duplex_payload_with_backpressure(self):
        child = self.start()
        to_uart = bytes(range(256)) * 2048
        from_uart = bytes(range(255, -1, -1)) * 2048
        errors = []
        def writer(fd, data):
            try:
                self.send(fd, data, timeout=12)
            except BaseException as error:
                errors.append(error)
        writers = [threading.Thread(target=writer, args=(self.input_w, to_uart)),
                   threading.Thread(target=writer, args=(self.master, from_uart))]
        for thread in writers:
            thread.start()
        time.sleep(0.25)  # both bounded queues should encounter backpressure
        results = {}
        def reader(name, fd, size):
            try:
                results[name] = self.read_exact(fd, size, timeout=12)
            except BaseException as error:
                errors.append(error)
        readers = [threading.Thread(target=reader, args=("tx", self.master, len(to_uart))),
                   threading.Thread(target=reader, args=("rx", self.output_r, len(from_uart)))]
        for thread in readers:
            thread.start()
        for thread in writers + readers:
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(results["tx"], to_uart)
        self.assertEqual(results["rx"], from_uart)
        self.close(self.input_w)
        self.finish(child)
        self.restored()

    def test_uart_hangup_with_full_rx_queue_is_bounded(self):
        fcntl.fcntl(self.output_w, fcntl.F_SETPIPE_SZ, 4096)
        child = self.start(interactive=True)
        self.send(self.master, b"x" * 32768)
        time.sleep(0.2)
        self.close(self.master)
        text = self.finish(child, code=1, timeout=5)
        self.assertIn(b"UART disconnected", text)
        self.assertIn(b"drain timed out", text)
        self.assertEqual(termios.tcgetattr(self.local), self.original_local)
        self.assertEqual(fcntl.fcntl(self.local, fcntl.F_GETFL), self.input_flags)
        self.assertEqual(fcntl.fcntl(self.output_w, fcntl.F_GETFL), self.output_flags)

    def test_eof_with_permanent_output_backpressure_is_bounded(self):
        fcntl.fcntl(self.output_w, fcntl.F_SETPIPE_SZ, 4096)
        child = self.start()
        errors = []
        # Less than typical PTY capacity plus console queue, but enough to fill
        # stdout's pipe without a reader. Sender has its own bounded timeout.
        def writer():
            try:
                self.send(self.master, b"x" * 32768, timeout=2)
            except AssertionError:
                pass  # kernel/console backpressure is exactly what is tested
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=writer)
        thread.start()
        time.sleep(0.2)
        self.close(self.input_w)
        start = time.monotonic()
        text = self.finish(child, code=1, timeout=5)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertIn(b"drain timed out", text)
        self.assertLess(time.monotonic() - start, 4)
        self.restored()

    def test_eof_with_permanent_uart_backpressure_is_bounded(self):
        child = self.start()
        self.send(self.input_w, b"x" * 32768)
        self.close(self.input_w)
        time.sleep(0.3)
        cpu = Path(f"/proc/{child.pid}/stat").read_text().split()
        self.assertLess((int(cpu[13]) + int(cpu[14])) / os.sysconf("SC_CLK_TCK"), 0.1)
        text = self.finish(child, code=1, timeout=5)
        self.assertIn(b"drain timed out", text)
        self.restored()

    def test_bad_arguments_before_uart_open(self):
        for args in (("--baud",), ("--baud", "12345"), ("--baud", "0"),
                     ("--baud", "-9600"), ("--baud", "115200junk"),
                     ("--baud", "999999999999999999999999"),
                     ("--baud", "9600", "--baud", "115200"),
                     ("--unknown",), ("extra-tty",)):
            with self.subTest(args=args):
                child = self.start(args=args, ready=False, harness=True)
                text = self.finish(child, code=2)
                self.assertIn(b"usage:", text)
                self.assertIn(b"HARNESS restored signals", text)
                self.assertNotIn(b"Ctrl-] exits on terminal stdin\n", text)
                self.restored()

    def test_same_uart_as_stdin_rejected_without_changing_termios(self):
        child = self.start(input_fd=self.slave, ready=False)
        text = self.finish(child, code=1)
        self.assertIn(b"must not be the UART", text)
        self.restored()

    def test_usage_documents_default_and_escape(self):
        result = subprocess.run([str(NATIVE)], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"console", result.stderr)
        self.assertIn(b"/dev/ttyAMA1", result.stderr)
        self.assertIn(b"115200", result.stderr)
        self.assertIn(b"Ctrl-]", result.stderr)
        self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
