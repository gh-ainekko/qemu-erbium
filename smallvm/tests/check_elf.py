#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Dependency-free ELF/ISA checks for the deliberately restricted RV64 port."""
import argparse
import json
import re
import struct
import subprocess
from pathlib import Path


def check(path, prefix):
    data = path.read_bytes()
    assert data[:7] == b'\x7fELF\x02\x01\x01', 'expected ELF64 little-endian'
    header = struct.unpack_from('<HHIQQQIHHHHHH', data, 16)
    kind, machine, _, entry, phoff, _, flags, _, phsize, phnum, *_ = header
    assert kind == 2 and machine == 243, 'expected static RISC-V executable'
    assert flags & 6 == 0, 'hard-float ELF ABI is forbidden'
    assert phsize == 56 and phnum > 0
    segments = []
    for i in range(phnum):
        typ, perm, off, va, pa, filesz, memsz, align = struct.unpack_from('<IIQQQQQQ', data, phoff+i*phsize)
        assert typ not in (2, 3), 'dynamic linking/interpreter forbidden'
        if typ != 1:
            continue
        assert filesz <= memsz and off+filesz <= len(data), 'invalid load segment'
        assert va == pa, 'physical and virtual addresses differ'
        assert 0x40000000 <= va and va+memsz <= 0x41000000, 'load segment outside MRAM'
        assert not (perm & 1 and perm & 2), 'writable executable segment'
        segments.append(dict(address=hex(va), filesz=filesz, memsz=memsz, flags=perm))
    assert any(s['flags'] & 1 and int(s['address'], 16) <= entry < int(s['address'], 16)+s['filesz'] for s in segments), 'entry not file-backed executable memory'
    attrs = subprocess.check_output([prefix+'readelf', '-A', str(path)], text=True)
    arch = re.search(r'Tag_RISCV_arch:\s*"([^"]+)"', attrs)
    assert arch, 'missing ISA attributes'
    extensions = arch[1].split('_')
    assert re.fullmatch(r'rv64i[0-9p]*', extensions[0]), arch[1]
    for ext in extensions[1:]:
        name = re.sub(r'[0-9].*', '', ext)
        assert name in {'m', 'c', 'zicsr', 'zifencei', 'zmmul'}, 'forbidden ISA extension: '+ext
    dis = subprocess.check_output([prefix+'objdump', '-d', str(path)], text=True)
    instructions = []
    for line in dis.splitlines():
        match = re.match(r'^\s*[0-9a-f]+:\s+[0-9a-f]+\s+([^\s]+)', line)
        if not match:
            continue
        mnemonic = match[1]
        assert not re.match(r'^(amo|lr\.|sc\.|f(?!ence)|v)', mnemonic), 'forbidden instruction: '+line
        assert mnemonic not in {'.word', '.4byte', '.2byte'}, 'unrecognized instruction: '+line
        instructions.append(mnemonic)
    assert instructions, 'no disassembly'
    return dict(elf=str(path), entry=hex(entry), architecture=arch[1],
                instruction_count=len(instructions), segments=segments, status='PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('elf', type=Path)
    parser.add_argument('--prefix', default='riscv64-unknown-elf-')
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.elf, args.prefix), indent=2))
    except (AssertionError, OSError, subprocess.CalledProcessError, struct.error) as exc:
        parser.exit(1, f'ELF validation failed: {exc}\n')


if __name__ == '__main__':
    main()
