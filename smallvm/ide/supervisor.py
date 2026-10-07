#!/usr/bin/python3
"""One native desktop + emulator session, with a bounded transparent PTY wire."""
import argparse
import datetime
import errno
import glob
import json
import os
from pathlib import Path
import pty
import select
import signal
import socket
import struct
import subprocess
import time
import tty

LIMIT = 1024 * 1024


class Session:
    def __init__(self, repo, runtime, logs):
        self.repo, self.runtime, self.logs = repo, runtime, logs
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.children, self.handles = {}, []
        self.stopping = False
        self.enabled = True
        self.emulator_restarts = 0
        self.pending = [bytearray(), bytearray()]
        self.totals = [0, 0]
        self.started = time.time()
        self.last_restart = 0
        self.masters, self.slaves = [], []
        for _ in range(2):
            master, slave = pty.openpty()
            tty.setraw(slave)
            os.set_blocking(master, False)
            self.masters.append(master)
            # Keep slaves open through IDE reconnects: no EIO/spin on masters.
            self.slaves.append(slave)
        self.link(self.runtime / "erbium-serial", Path(os.ttyname(self.slaves[0])))
        self.link(self.runtime / "emulator-serial", Path(os.ttyname(self.slaves[1])))
        self.uart_logs = [
            self.open_log("uart-ide-to-emulator.bin"),
            self.open_log("uart-emulator-to-ide.bin"),
        ]
        self.events = self.open_log("events.jsonl")
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.control_path = self.runtime / "control.sock"
        self.control_path.unlink(missing_ok=True)
        self.control.bind(str(self.control_path))
        os.chmod(self.control_path, 0o600)
        self.control.listen(4)
        self.control.setblocking(False)

    @staticmethod
    def link(path, target):
        temporary = path.with_name(path.name + ".new")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        temporary.replace(path)

    def open_log(self, name):
        handle = (self.logs / name).open("ab", buffering=0)
        self.handles.append(handle)
        return handle

    def event(self, kind, **details):
        self.events.write((json.dumps(dict(time=time.time(), kind=kind, **details)) + "\n").encode())

    def spawn(self, name, command, cwd=None):
        self.event("spawn", process=name, command=command)
        with (self.logs / (name + ".stdout.log")).open("ab", buffering=0) as out:
            with (self.logs / (name + ".stderr.log")).open("ab", buffering=0) as err:
                self.children[name] = subprocess.Popen(
                    command, cwd=cwd or self.repo, stdin=subprocess.DEVNULL,
                    stdout=out, stderr=err,
                    start_new_session=True, close_fds=True,
                )
        return self.children[name]

    def terminate(self, name):
        proc = self.children.pop(name, None)
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        if proc:
            self.event("exit", process=name, returncode=proc.returncode)

    def emulator(self):
        elf = self.repo / "build/smallvm/smallvm.elf"
        header = elf.read_bytes()[:64]
        if header[:6] != b"\x7fELF\x02\x01":
            raise RuntimeError("expected a little-endian ELF64 firmware")
        entry = struct.unpack_from("<Q", header, 24)[0]
        uart = os.ttyname(self.slaves[1])
        self.last_restart = time.monotonic()
        self.spawn("emulator", [
            str(self.repo / "dist/bin/erbium_emu"),
            "-elf_load", str(elf), "-reset_pc", hex(entry),
            "-minions", "1", "-single_thread", "-max_cycles", "-1",
            "-uart_rx_file", uart, "-uart_tx_file", uart,
        ])

    def status(self):
        try:
            ide = json.loads((self.runtime / "ide-status.json").read_text())
        except (OSError, ValueError):
            ide = None
        return dict(
            started=self.started, logs=str(self.logs),
            ide_serial=str(self.runtime / "erbium-serial"),
            ide_pty=os.ttyname(self.slaves[0]),
            emulator_pty=os.ttyname(self.slaves[1]),
            emulator_enabled=self.enabled, emulator_restarts=self.emulator_restarts,
            bytes_ide_to_emulator=self.totals[0],
            bytes_emulator_to_ide=self.totals[1],
            queued_to_ide=len(self.pending[0]),
            queued_to_emulator=len(self.pending[1]),
            relay_queue_limit=LIMIT,
            ide=ide,
            processes={name: dict(pid=proc.pid, returncode=proc.poll())
                       for name, proc in self.children.items()},
        )

    def flush_wire(self):
        for queue in self.pending:
            queue.clear()
        for slave in self.slaves:
            import termios
            termios.tcflush(slave, termios.TCIOFLUSH)
        for master in self.masters:
            while True:
                try:
                    if not os.read(master, 65536):
                        break
                except BlockingIOError:
                    break

    def command(self, command):
        if command == "status":
            return self.status()
        if command not in ("reset", "disconnect", "reconnect"):
            return {"error": "allowed commands: status, reset, disconnect, reconnect"}
        self.event("control", command=command)
        if command in ("reset", "disconnect"):
            self.enabled = False
            self.terminate("emulator")
            self.flush_wire()
        if command in ("reset", "reconnect"):
            self.enabled = True
            if "emulator" not in self.children:
                self.emulator_restarts += 1
                self.emulator()
        self.request_ide("disconnect" if command == "disconnect" else "reconnect")
        return self.status()

    def request_ide(self, command):
        request = self.runtime / "ide-request.txt"
        temporary = request.with_suffix(".new")
        temporary.write_text(command)
        temporary.replace(request)

    def desktop(self):
        env = os.environ
        env["DISPLAY"] = ":87"
        env["XAUTHORITY"] = str(self.runtime / "Xauthority")
        env["SDL_AUDIODRIVER"] = "dummy"
        env["PYTHONUNBUFFERED"] = "1"
        # Private X11 cookie. Disable TCP; all desktop clients share this UID.
        subprocess.run(["xauth", "-f", env["XAUTHORITY"], "add", ":87",
                        "MIT-MAGIC-COOKIE-1", os.urandom(16).hex()], check=True)
        self.spawn("xvfb", ["Xvfb", ":87", "-screen", "0", "1280x800x24",
                           "-nolisten", "tcp", "-auth", env["XAUTHORITY"]])
        for _ in range(100):
            if subprocess.run(["xdpyinfo"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                break
            if self.children["xvfb"].poll() is not None:
                raise RuntimeError("Xvfb failed; see its stderr log")
            time.sleep(.1)
        else:
            raise RuntimeError("Xvfb did not become ready")
        self.spawn("openbox", ["openbox", "--config-file",
                              str(self.repo / "smallvm/ide/openbox.xml")])
        gp = self.repo / "ext/smallvm/gp"
        self.spawn("ide", [str(gp / "gp-linux64bit")] +
                   sorted(glob.glob(str(gp / "runtime/lib/*.gp"))) +
                   ["loadIDE.gp", str(self.repo / "smallvm/ide/native-compat.gp"),
                    str(self.repo / "smallvm/ide/launch.gp")], cwd=gp)
        self.spawn("vnc", [
            "x11vnc", "-display", ":87", "-auth", env["XAUTHORITY"],
            "-localhost", "-listen", "127.0.0.1", "-rfbport", "5900",
            "-forever", "-shared", "-nopw", "-noxdamage", "-quiet",
        ])
        web = self.runtime / "web"
        web.mkdir(exist_ok=True)
        self.link(web / "index.html", self.repo / "smallvm/ide/index.html")
        self.link(web / "novnc", Path("/usr/share/novnc"))
        self.spawn("frontend", ["/usr/bin/python3",
                   str(self.repo / "smallvm/ide/frontend.py"), "--web", str(web)])

    def relay(self, timeout=.1):
        reads = [fd for i, fd in enumerate(self.masters)
                 if len(self.pending[1-i]) < LIMIT]
        writes = [fd for i, fd in enumerate(self.masters) if self.pending[i]]
        ready, writable, _ = select.select([self.control] + reads, writes, [], timeout)
        for i, master in enumerate(self.masters):
            if master in ready:
                size = min(65536, LIMIT - len(self.pending[1-i]))
                try:
                    data = os.read(master, size)
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EIO):
                        raise
                    data = b""
                if data:
                    self.uart_logs[i].write(data)
                    self.totals[i] += len(data)
                    self.pending[1-i].extend(data)
            if master in writable and self.pending[i]:
                try:
                    sent = os.write(master, self.pending[i])
                    del self.pending[i][:sent]
                except BlockingIOError:
                    pass
        return self.control in ready

    def run(self):
        self.emulator()
        self.desktop()
        published = 0.0
        while not self.stopping:
            # Any desktop failure tears down this whole owned session; systemd
            # recreates its paired PTYs and all children together.
            for name, proc in list(self.children.items()):
                if proc.poll() is not None:
                    if name != "emulator":
                        raise RuntimeError(f"{name} exited ({proc.returncode})")
                    self.terminate(name)
            if self.enabled and "emulator" not in self.children:
                if time.monotonic() - self.last_restart > 3:
                    self.emulator_restarts += 1
                    self.flush_wire()
                    self.emulator()
                    self.request_ide("reconnect")
            if self.relay():
                conn, _ = self.control.accept()
                # Only same-UID/root callers can open this 0600 UNIX socket.
                with conn:
                    conn.settimeout(.25)
                    try:
                        request = conn.recv(128).decode("ascii").strip()
                        result = self.command(request)
                        conn.sendall((json.dumps(result) + "\n").encode())
                    except (OSError, ValueError) as exc:
                        self.event("control_error", error=str(exc))

            if time.monotonic() - published > 1:
                status = self.runtime / "status.json"
                temp = status.with_suffix(".new")
                temp.write_text(json.dumps(self.status(), indent=2) + "\n")
                temp.replace(status)
                published = time.monotonic()

    def close(self):
        self.stopping = True
        for name in reversed(list(self.children)):
            self.terminate(name)
        self.control.close()
        for fd in self.masters + self.slaves:
            os.close(fd)
        for name in ("control.sock", "erbium-serial", "emulator-serial"):
            (self.runtime / name).unlink(missing_ok=True)
        for handle in self.handles:
            handle.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, default=Path("/run/smallvm-ide"))
    args = parser.parse_args()
    logs = args.repo / "build/smallvm-ide"
    logs.mkdir(parents=True, exist_ok=True)
    session_logs = logs / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    session = Session(args.repo.resolve(), args.runtime, session_logs)
    Session.link(logs / "current", session_logs)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: setattr(session, "stopping", True))
    try:
        session.run()
    finally:
        session.close()


if __name__ == "__main__":
    main()
