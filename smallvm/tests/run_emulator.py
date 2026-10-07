#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Bounded standalone Erbium tests; UART bytes and emulator logs stay separate."""
import argparse
import errno
import hashlib
import json
import os
import pty
import re
import select
import struct
import subprocess
import time
import tty
from pathlib import Path


def frames(data):
    """Decode complete board-to-IDE frames; reject stray unframed bytes."""
    result = []
    pos = 0
    while pos < len(data):
        marker = data[pos]
        if marker not in (250, 251):
            raise AssertionError(f'unframed UART byte at {pos}: {marker:02x}')
        if len(data)-pos < 3:
            break
        typ, ident = data[pos+1:pos+3]
        if marker == 250:
            result.append((typ, ident, b''))
            pos += 3
        else:
            if len(data)-pos < 5:
                break
            size = int.from_bytes(data[pos+3:pos+5], 'little')
            if len(data)-pos < 5+size:
                break
            result.append((typ, ident, bytes(data[pos+5:pos+5+size])))
            pos += 5+size
    return result


def run(emu, elf, output, mode, timeout, minions, poison_mram):
    output.mkdir(parents=True, exist_ok=True)
    # The reset PC comes from the actual ELF, never a guessed text base.
    blob = elf.read_bytes()
    assert blob[:6] == b'\x7fELF\x02\x01'
    if mode in ('fault', 'no-bss'):
        syms = subprocess.check_output(['riscv64-unknown-elf-nm', str(elf)], text=True)
        symbol = 'smallvm_selftest' if mode == 'fault' else '_start'
        match = re.search(r'^([0-9a-f]+) T '+symbol+r'$', syms, re.MULTILINE)
        assert match, 'fault test requires a selftest ELF'
        target = int(match[1], 16)
        phoff = struct.unpack_from('<Q', blob, 32)[0]
        phsize, phnum = struct.unpack_from('<HH', blob, 54)
        image = bytearray(blob)
        mutated = False
        for i in range(phnum):
            typ, perm, off, va, _, filesz, _, _ = struct.unpack_from('<IIQQQQQQ', blob, phoff+i*phsize)
            if typ == 1 and perm & 1 and va <= target < va+filesz-4:
                pos = off+target-va
                if mode == 'fault':
                    image[pos:pos+4] = b'\x00'*4  # illegal instruction after UART setup
                else:
                    # Match the startup's one exact RV64 "sd zero,0(t0)" instruction.
                    instruction = bytes.fromhex('23b00200')
                    startup = image[pos:pos+128]
                    assert startup.count(instruction) == 1, 'cannot identify BSS clear store'
                    delta = startup.index(instruction)
                    pos += delta
                    target += delta
                    image[pos:pos+4] = bytes.fromhex('13000000')  # nop instead of store
                    poison_mram = True
                mutated = True
                break
        assert mutated, 'fault injection address outside executable segment'
        (output/'mutation.json').write_text(json.dumps(dict(
            original_elf=str(elf), original_sha256=hashlib.sha256(blob).hexdigest(),
            mutation_pc=hex(target), file_offset=pos), indent=2)+'\n')
        elf = output/(mode+'.elf')
        elf.write_bytes(image)
        blob = image
    entry = struct.unpack_from('<Q', blob, 24)[0]
    master, slave = pty.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    uart = os.ttyname(slave)
    command = [str(emu), '-elf_load', str(elf), '-reset_pc', hex(entry),
               '-minions', minions, '-single_thread', '-max_cycles', '-1',
               '-uart_rx_file', uart, '-uart_tx_file', uart]
    if poison_mram:
        mram = output/'poisoned-mram.img'
        mram.write_bytes(b'\xa5'*(16*1024*1024))
        command += ['--mram-file', str(mram)]
    received = bytearray()
    last_command = 0.0
    success = False
    started = time.monotonic()
    proc = None
    try:
        with (output/'emulator.log').open('wb') as log:
            proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            while time.monotonic()-started < timeout:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    try:
                        received.extend(os.read(master, 65536))
                    except OSError as exc:
                        if exc.errno not in (errno.EIO, errno.EAGAIN):
                            raise
                if mode == 'no-bss':
                    if b'SmallVM selftest FAIL startup-data-bss-tls\n' in received:
                        success = True
                        break
                elif mode == 'fault':
                    pattern = (rb'SmallVM FAULT cause/pc/value/sp: 0000000000000002 '
                               rb'([0-9a-f]{16}) ([0-9a-f]{16}) ([0-9a-f]{16})[\r\n]')
                    match = re.search(pattern, received)
                    if match:
                        assert int(match[1], 16) == target, 'wrong fault PC'
                        assert int(match[2], 16) == 0, 'wrong illegal instruction mtval'
                        assert 0x40000000 <= int(match[3], 16) < 0x41000000, 'fault stack outside MRAM'
                        success = True
                        break
                elif mode == 'selftest':
                    if (b'SMALLVM_SELFTEST FAIL' in received or b'SmallVM FAULT' in received
                            or b'SmallVM selftest FAIL' in received):
                        raise AssertionError('firmware reported a failure or trap')
                    if (re.search(rb'SMALLVM_SELFTEST PASS checks=\d+ failures=0[\r\n]', received)
                            and b'SmallVM selftest PASS\n' in received):
                        assert received.count(b'SmallVM selftest BEGIN') == 1, 'multiple harts initialized VM'
                        success = True
                        break
                else:
                    decoded = frames(received)
                    now = time.monotonic()
                    # Firmware is correctly silent until contacted by an IDE.
                    # Retry a short handshake if the initial request preceded boot.
                    if now-started > 0.1 and now-last_command > 1:
                        # Six bytes fit even in the emulator's small RX FIFO.
                        os.write(master, bytes([250, 26, 73, 250, 12, 0]))
                        last_command = now
                    ping = any(typ == 26 for typ, _, _ in decoded)
                    version = any(typ == 22 and b'Erbium' in body for typ, _, body in decoded)
                    if ping and version:
                        success = True
                        break
                if proc.poll() is not None:
                    raise AssertionError(f'emulator exited before success: {proc.returncode}')
            if not success:
                raise AssertionError(f'{mode} timed out after {timeout}s')
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
        (output/'uart.bin').write_bytes(received)
        (output/'uart.txt').write_text(received.decode('utf-8', errors='backslashreplace'))
        result = dict(mode=mode, status='PASS' if success else 'FAIL', command=command,
                      elf_sha256=hashlib.sha256(blob).hexdigest(),
                      emulator_sha256=hashlib.sha256(emu.read_bytes()).hexdigest(),
                      elapsed_seconds=round(time.monotonic()-started, 3), uart_bytes=len(received))
        (output/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('elf', type=Path)
    parser.add_argument('--emu', type=Path, default=root/'dist/bin/erbium_emu')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=['selftest', 'protocol', 'fault', 'no-bss'], default='selftest')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--minions', default='0x1')
    parser.add_argument('--poison-mram', action='store_true')
    args = parser.parse_args()
    try:
        run(args.emu.resolve(), args.elf.resolve(), args.output.resolve(), args.mode,
            args.timeout, args.minions, args.poison_mram)
    except (AssertionError, OSError) as exc:
        parser.exit(1, f'Emulator test failed: {exc}; see {args.output}\n')


if __name__ == '__main__':
    main()
