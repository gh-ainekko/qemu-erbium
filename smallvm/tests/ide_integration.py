#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""REAL native MicroBlocks IDE/compiler + isolated Erbium UART integration.

No hand-built VM bytecode. GP loads every runtime library and loadIDE.gp, then
our last-loaded startup driver. Two raw PTYs isolate this run from the live IDE.
The bounded relay preserves partial writes and records both directions.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import time
import tty


ROOT = Path(__file__).resolve().parents[2]


def generate_project(path):
    """24 near-1KB source chunks, plus runnable functions/list/timer workload."""
    showcase = (ROOT / "smallvm/examples/Erbium Showcase.ubp").read_text()
    # Emulator simulation time need not track wall time. Keep the user-facing
    # showcase paced at 100ms, but accelerate the automated timer workload.
    showcase = showcase.replace("waitMillis 100", "waitMillis 5")
    # Unique literals prevent identical CRCs from hiding omitted chunks.
    for i in range(24):
        literal = f"UART-STRESS-{i:02d}:" + "".join(
            chr(65 + (j + i) % 26) for j in range(900))
        showcase += f"\nscript {40 + i % 4 * 300} {400 + i // 4 * 120} {{\n"
        showcase += f"status = '{literal}'\n}}\n"
    path.write_text(showcase)
    path.with_name("edited.ubp").write_text(showcase.replace(
        "return ((n * 2) + 1)", "return ((n * 2) + 2)"))


def frames(data):
    """Complete UART frames, leaving incomplete tails for later inspection."""
    result = []
    pos = 0
    while pos + 3 <= len(data):
        marker, opcode, ident = data[pos:pos + 3]
        if marker not in (250, 251):
            raise AssertionError(f"unframed UART byte {marker:02x} at {pos}")
        size = 0 if marker == 250 else None
        if size is None:
            if pos + 5 > len(data):
                break
            size = int.from_bytes(data[pos + 3:pos + 5], "little")
        header = 3 if marker == 250 else 5
        if pos + header + size > len(data):
            break
        result.append((opcode, ident, bytes(data[pos + header:pos + header + size])))
        pos += header + size
    return result, len(data) - pos


def assert_clean_gp_log(text):
    """A later PASS must never conceal parser/debugger or serial errors."""
    patterns = (
        r"(?m)^.*\.gp:\d+\b",
        r"(?mi)^(?:undefined|syntax error|parse error|serial error|file not found:|stopped at|to debug, type:).*$",
        r"(?m)^-{8,}\s*$",
        r"(?m)^(?:Welcome to GP!|gp>).*$",
    )
    errors = [match.group(0) for pattern in patterns for match in re.finditer(pattern, text)]
    assert not errors, f"GP parser/runtime error in log: {errors[:5]}"


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    generate_project(output / "large.ubp")
    config = dict(output=str(output), project=str(output / "large.ubp"),
                  edited=str(output / "edited.ubp"))
    gpdir = args.gp.parent.resolve()
    elf = args.elf.resolve()
    emu = args.emu.resolve()
    blob = elf.read_bytes()
    assert blob[:6] == b"\x7fELF\x02\x01", "expected ELF64 little-endian firmware"
    entry = struct.unpack_from("<Q", blob, 24)[0]
    pairs = [pty.openpty(), pty.openpty()]
    for master, slave in pairs:
        tty.setraw(slave)
        os.set_blocking(master, False)
    config["port"] = os.ttyname(pairs[1][1])
    (output / "config.json").write_text(json.dumps(config))
    emulator_command = [str(emu), "-elf_load", str(elf), "-reset_pc", hex(entry),
                        "-minions", "0x1", "-single_thread", "-max_cycles", "-1",
                        "-uart_rx_file", os.ttyname(pairs[0][1]),
                        "-uart_tx_file", os.ttyname(pairs[0][1])]
    driver = Path(__file__).with_suffix(".gp")
    gp_command = ["xvfb-run", "-a", "-s", "-screen 0 1280x900x24",
                  str(args.gp.resolve())]
    gp_command += [str(p.relative_to(gpdir)) for p in sorted((gpdir / "runtime/lib").glob("*.gp"))]
    gp_command += ["loadIDE.gp", str(args.native_compat.resolve())]
    gp_command += [str(driver), "-", "--ide-test-config",
                   str(output / "config.json")]
    processes = []
    captures = {"ide-to-board": bytearray(), "board-to-ide": bytearray()}
    # Destination queues. Bound memory independently of emulator/IDE progress.
    queues = {p[0]: bytearray() for p in pairs}
    source_names = {pairs[0][0]: "board-to-ide", pairs[1][0]: "ide-to-board"}
    destinations = {pairs[0][0]: pairs[1][0], pairs[1][0]: pairs[0][0]}
    started = time.monotonic()
    success = False
    failure = None
    result = {}
    relay_peak = 0
    try:
        with (output / "emulator.log").open("wb") as elog, (output / "gp.log").open("wb") as glog:
            ep = subprocess.Popen(emulator_command, stdout=elog, stderr=subprocess.STDOUT,
                                  start_new_session=True)
            processes.append(ep)
            gp = subprocess.Popen(gp_command, cwd=gpdir, stdout=glog, stderr=subprocess.STDOUT,
                                  start_new_session=True)
            processes.append(gp)
            while time.monotonic() - started < args.timeout:
                readable = [fd for fd in queues if len(queues[destinations[fd]]) < 1024 * 1024]
                writable = [fd for fd, queue in queues.items() if queue]
                reads, writes, _ = select.select(readable, writable, [], 0.01)
                for fd in reads:
                    try:
                        data = os.read(fd, 65536)
                    except OSError as exc:
                        if exc.errno not in (errno.EIO, errno.EAGAIN):
                            raise
                        continue
                    captures[source_names[fd]].extend(data)
                    queues[destinations[fd]].extend(data)
                    assert len(captures[source_names[fd]]) < 16 * 1024 * 1024, "UART capture limit exceeded"
                for fd in writes:
                    try:
                        # Optional deliberately fragmented transport tests partial-frame handling.
                        count = os.write(fd, queues[fd][:args.relay_write_size])
                    except BlockingIOError:
                        continue
                    del queues[fd][:count]
                relay_peak = max(relay_peak, *(len(q) for q in queues.values()))
                if ep.poll() is not None:
                    raise AssertionError(f"emulator exited early: {ep.returncode}")
                if gp.poll() is not None:
                    # Flush remaining emitted bytes before checking transcripts.
                    for _ in range(10):
                        for fd in queues:
                            try:
                                data = os.read(fd, 65536)
                                captures[source_names[fd]].extend(data)
                            except OSError:
                                pass
                        time.sleep(0.01)
                    assert gp.returncode == 0, f"GP exited {gp.returncode}"
                    assert_clean_gp_log((output / "gp.log").read_text(errors="replace"))
                    gp_result = output / "gp-result.json"
                    assert gp_result.exists(), "GP exited without result; inspect gp.log"
                    result = json.loads(gp_result.read_text())
                    assert result.get("status") == "PASS", result
                    outgoing, tail = frames(captures["ide-to-board"])
                    incoming, incoming_tail = frames(captures["board-to-ide"])
                    assert not tail and not incoming_tail, "incomplete captured UART frames"
                    delays = [body[0] for op, ident, body in outgoing if op == 30 and ident == 1 and body]
                    assert len(delays) >= 2 and all(value == 1 for value in delays), (
                        f"Erbium default serial profile did not select delay 1 on connect/reconnect: {delays}")
                    downloads = [body for op, _, body in outgoing if op == 32]
                    assert len(downloads) >= 24, "missing compiled chunk downloads"
                    expected_downloads = (result["chunk_count"]
                                          + result["incrementally_changed_chunks"]
                                          + result["recompiled_chunk_count"])
                    assert len(downloads) == expected_downloads, (
                        f"unexpected redundant downloads: {len(downloads)} != {expected_downloads}")
                    assert sum(len(x) for x in downloads) > 16 * 1024, "upload did not stress >16KB"
                    assert any(len(x) >= 900 for x in downloads), "no near-1KB chunk"
                    assert any(op == 21 for op, _, _ in incoming), "no actual board variable replies"
                    assert not any(op == 19 for op, _, _ in incoming), "board reported taskErrorMsg"
                    readbacks = [(ident, body) for op, ident, body in incoming if op == 32]
                    assert len(readbacks) == result["chunk_count"], "wrong readback chunk count"
                    assert len({ident for ident, _ in readbacks}) == len(readbacks), "duplicate readback chunk"
                    result.update(downloaded_chunk_frames=len(downloads),
                                  downloaded_chunk_bytes=sum(len(x) for x in downloads),
                                  incoming_frames=len(incoming), outgoing_frames=len(outgoing))
                    result["redundant_chunk_downloads"] = 0
                    result["readback_chunk_frames"] = len(readbacks)
                    result["gp_error_log_checks"] = "PASS"
                    success = True
                    break
            if not success:
                raise AssertionError(f"IDE integration timed out after {args.timeout}s")
    except (AssertionError, OSError, ValueError) as exc:
        failure = str(exc)
    finally:
        for proc in reversed(processes):
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        for master, slave in pairs:
            os.close(master)
            os.close(slave)
        for name, data in captures.items():
            (output / f"{name}.bin").write_bytes(data)
        # Preserve wire diagnostics on failure too, especially default-profile
        # regressions before the version identifies boardType.
        try:
            outgoing, _ = frames(captures["ide-to-board"])
            incoming, _ = frames(captures["board-to-ide"])
            result["observed_serial_delay_requests"] = [
                body[0] for op, ident, body in outgoing if op == 30 and ident == 1 and body
            ]
            result["captured_readback_chunks"] = sum(op == 32 for op, _, _ in incoming)
        except AssertionError as exc:
            result["capture_decode_error"] = str(exc)
        result.update(status="PASS" if success else "FAIL", failure=failure,
                      elapsed_seconds=round(time.monotonic() - started, 3),
                      emulator_command=emulator_command, gp_command=gp_command,
                      elf_sha256=hashlib.sha256(blob).hexdigest(),
                      emulator_sha256=hashlib.sha256(emu.read_bytes()).hexdigest(),
                      gp_sha256=hashlib.sha256(args.gp.read_bytes()).hexdigest(),
                      relay_queue_peak=relay_peak, relay_write_size=args.relay_write_size,
                      uart_bytes={k: len(v) for k, v in captures.items()})
        result["native_compat_sha256"] = hashlib.sha256(args.native_compat.read_bytes()).hexdigest()
        result["driver_sha256"] = hashlib.sha256(driver.read_bytes()).hexdigest()
        result["project_sha256"] = hashlib.sha256((output / "large.ubp").read_bytes()).hexdigest()
        result["ide_source_sha256"] = {
            name: hashlib.sha256((gpdir.parent / "ide" / name).read_bytes()).hexdigest()
            for name in ("MicroBlocksCompiler.gp", "MicroBlocksRuntime.gp",
                         "MicroBlocksDecompiler.gp", "MicroBlocksProject.gp",
                         "MicroBlocksEditor.gp")
        }
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", nargs="?", type=Path, default=ROOT / "build/smallvm/smallvm.elf")
    parser.add_argument("--emu", type=Path, default=ROOT / "dist/bin/erbium_emu")
    parser.add_argument("--gp", type=Path, default=ROOT / "ext/smallvm/gp/gp-linux64bit")
    parser.add_argument("--native-compat", type=Path, default=ROOT / "smallvm/ide/native-compat.gp",
                        help="same native UI compatibility layer used by the live IDE; no launch/port overrides")
    parser.add_argument("--output", type=Path, default=ROOT / "build/smallvm/ide-tests")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--relay-write-size", type=int, default=4096)
    args = parser.parse_args()
    if not shutil.which("xvfb-run"):
        parser.error("xvfb-run is required (install xvfb); this test never uses the live display")
    if args.relay_write_size < 1:
        parser.error("--relay-write-size must be positive")
    for path in (args.elf, args.emu, args.gp, args.native_compat):
        if not path.is_file():
            parser.error(f"required artifact does not exist: {path}")
    # Prevent stale success artifacts from satisfying a failed invocation.
    args.output.mkdir(parents=True, exist_ok=True)
    for name in ("gp-result.json", "saved.ubp", "readback.json", "decompiled.ubp", "save-dialog.png"):
        (args.output / name).unlink(missing_ok=True)
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
