"""Inspect actual run-linux argv without starting QEMU or requiring build artifacts."""
import json
import os
from pathlib import Path
import subprocess
import signal
import time
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.qemu = self.path / "qemu"
        self.qemu.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        self.qemu.chmod(0o755)
        self.image = self.path / "Image"
        self.image.write_bytes(b"kernel")
        self.mram = self.path / "mram"

    def run_script(self, *args):
        return subprocess.run(
            ["bash", str(ROOT / "scripts/run-linux.sh"), "--mram", str(self.mram), *args],
            env=dict(os.environ, QEMU=str(self.qemu), IMAGE=str(self.image)),
            capture_output=True, text=True, timeout=5)

    def test_uart_and_control_are_independent(self):
        p = self.run_script("--backend", "/tmp/control.sock", "--uart-socket", "/tmp/uart.sock", "--uart-test")
        self.assertEqual(p.returncode, 0, p.stderr)
        args = json.loads(p.stdout)
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "-serial"],
                         ["mon:stdio", "chardev:erb-uart"])
        channels = [args[i + 1] for i, a in enumerate(args) if a == "-chardev"]
        self.assertIn("socket,id=erb,path=/tmp/control.sock", channels)
        self.assertIn("socket,id=erb-uart,path=/tmp/uart.sock,reconnect-ms=1000", channels)
        self.assertIn("erbium-xspi.chardev=erb", args)
        cmdline = args[args.index("-append") + 1]
        self.assertIn("console=ttyAMA0", cmdline)
        self.assertNotIn("console=ttyAMA1", cmdline)
        self.assertIn("erbium.uarttest erbium.poweroff", cmdline)

    def test_no_uart_unchanged(self):
        p = self.run_script("--test")
        self.assertEqual(p.returncode, 0, p.stderr)
        args = json.loads(p.stdout)
        self.assertEqual(args.count("-serial"), 1)
        self.assertNotIn("-chardev", args)

    def test_standalone_console_without_control_backend(self):
        p = self.run_script("--uart-socket", "/tmp/uart.sock")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("chardev:erb-uart", json.loads(p.stdout))

    def test_kotama_mode(self):
        p = self.run_script("--backend", "/tmp/control", "--uart-socket", "/tmp/serial", "--kotama-test")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("erbium.kotamatest", p.stdout)

    def test_bad_arguments_do_not_launch_or_create_mram(self):
        for args in [("--uart-test",), ("--kotama-test", "--backend", "/tmp/c"),
                     ("--test", "--load-test"), ("--uart-socket",),
                     ("--backend",), ("--uart-socket", "/tmp/x,server=on")]:
            with self.subTest(args=args):
                p = self.run_script(*args)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertFalse(self.mram.exists())

    def test_pid_only_termination_reaps_backend_and_qemu(self):
        backend = self.path / "backend"
        backend.write_text("""#!/usr/bin/env python3
import os,socket,sys,time
from pathlib import Path
Path(os.environ['PID_BACKEND']).write_text(str(os.getpid()))
socks=[]
for key in ('--api-socket','--uart-socket'):
 s=socket.socket(socket.AF_UNIX);s.bind(sys.argv[sys.argv.index(key)+1]);s.listen(1);socks.append(s)
while True: time.sleep(1)
""")
        backend.chmod(0o755)
        self.qemu.write_text("""#!/usr/bin/env python3
import os,time
from pathlib import Path
Path(os.environ['PID_QEMU']).write_text(str(os.getpid()))
while True: time.sleep(1)
""")
        for mode in ([], ["--test"]):
            with self.subTest(mode=mode):
                files = [self.path / "backend.pid", self.path / "qemu.pid"]
                for f in files: f.unlink(missing_ok=True)
                env = dict(os.environ, EMU=str(backend), QEMU=str(self.qemu),
                           IMAGE=str(self.image), PID_BACKEND=str(files[0]), PID_QEMU=str(files[1]))
                p = subprocess.Popen(["bash", str(ROOT / "scripts/run-uart.sh"), *mode],
                                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True)
                pids = []
                try:
                    until = time.monotonic() + 5
                    while not all(f.exists() for f in files) and time.monotonic() < until:
                        time.sleep(.02)
                    self.assertTrue(all(f.exists() for f in files), "mock QEMU never launched")
                    pids = [int(f.read_text()) for f in files]
                    p.terminate()  # deliberately the runner PID, NOT its group
                    out, err = p.communicate(timeout=4)
                    self.assertEqual(p.returncode, 143, out + err)
                    for pid in pids:
                        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
                finally:
                    if p.poll() is None:
                        os.killpg(p.pid, signal.SIGKILL)
                        p.communicate(timeout=3)
                    for pid in pids:
                        try: os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError: pass


if __name__ == "__main__":
    unittest.main()
