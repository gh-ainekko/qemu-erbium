"""Fast offline tests of the real erbctl CLI and MRAM loader.

Run: python3 -m unittest discover -s tests -p 'test_erbctl_loader.py' -v

Requirements: Python stdlib, native gcc, host Linux libc/UAPI headers (including
mtd/mtd-user.h). No libelf, RISC-V compiler, kernel build, device nodes, root,
QEMU, network, or firmware files are needed. Both binaries and synthetic ELF
fixtures live in temporary directories. The native CLI links the two production
sources normally; a second binary includes those same sources with syscall-only
macros (see erbctl_mock.c) to exercise actual upload/verification/error cleanup.

Each malformed ELF is tested both with --check and as a normal load, against a
nonexistent device; its invalid-ELF diagnostic must precede any device error.
The device model additionally proves *zero* hardware opens for every malformed
fixture. Faults are one-shot to permit cleanup; this cannot simulate power loss,
SIGKILL, noncooperating raw MTD users, or hardware execution readiness.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MRAM_BASE = 0x40000000
MRAM_SIZE = 0x1000000
U64_MAX = (1 << 64) - 1
EHDR = struct.Struct("<16sHHIQQQIHHHHHH")
PHDR = struct.Struct("<IIQQQQQQ")
SHDR = struct.Struct("<IIQQQQIIQQ")
CODE = bytes(range(16))
_build = None
NATIVE = MOCK = None


def setUpModule():
    global _build, NATIVE, MOCK
    if not shutil.which("gcc"):
        raise unittest.SkipTest("native gcc is required for offline loader tests")
    _build = tempfile.TemporaryDirectory(prefix="erbctl-tests-build-")
    NATIVE = Path(_build.name) / "erbctl"
    MOCK = Path(_build.name) / "erbctl-mock"
    common = ["gcc", "-std=gnu11", "-O2", "-Wall", "-Wextra"]
    try:
        for target, sources in (
            (NATIVE, [ROOT / "linux/tools/erbctl.c",
                      ROOT / "linux/tools/erbium-loader.c"]),
            (MOCK, [ROOT / "tests/erbctl_mock.c"]),
        ):
            result = subprocess.run(
                common + [str(p) for p in sources] + ["-o", str(target)],
                text=True, capture_output=True, timeout=30)
            if result.returncode:
                raise AssertionError("native gcc build failed:\n" + result.stderr)
    except BaseException:
        _build.cleanup()
        raise


def tearDownModule():
    if _build:
        _build.cleanup()


@dataclass
class Segment:
    addr: int = MRAM_BASE
    data: bytes = CODE
    memsz: int = 48
    flags: int = 5  # PF_R | PF_X
    align: int = 1
    offset: int | None = None
    vaddr: int | None = None
    kind: int = 1  # PT_LOAD


def elf(segments=None, entry=MRAM_BASE, sections=()):
    """Build ELF64 directly: no assembler, linker, pyelftools or external input."""
    segments = [Segment()] if segments is None else segments
    out = bytearray(EHDR.size + PHDR.size * len(segments))
    headers = []
    for segment in segments:
        offset = segment.offset
        if offset is None:
            offset = (max(0x200, len(out)) + 15) & ~15
        end = offset + len(segment.data)
        if len(out) < end:
            out.extend(b"\0" * (end - len(out)))
        out[offset:end] = segment.data
        headers.append(PHDR.pack(
            segment.kind, segment.flags, offset,
            segment.addr if segment.vaddr is None else segment.vaddr,
            segment.addr, len(segment.data), segment.memsz, segment.align))
    shoff = 0
    if sections:
        shoff = (len(out) + 7) & ~7
        out.extend(b"\0" * (shoff - len(out)))
        for kind in sections:
            out.extend(SHDR.pack(0, kind, 0, 0, 0, 0, 0, 0, 1, 0))
    ident = b"\x7fELF" + bytes((2, 1, 1, 0)) + b"\0" * 8
    out[:EHDR.size] = EHDR.pack(
        ident, 2, 243, 1, entry, EHDR.size, shoff, 0,
        EHDR.size, PHDR.size, len(headers), SHDR.size if sections else 0,
        len(sections), 0)
    for i, header in enumerate(headers):
        start = EHDR.size + i * PHDR.size
        out[start:start + PHDR.size] = header
    return bytes(out)


def mutate(blob, offset, fmt, value):
    out = bytearray(blob)
    struct.pack_into("<" + fmt, out, offset, value)
    return bytes(out)


EH_FIELDS = {
    "type": (16, "H"), "machine": (18, "H"), "version": (20, "I"),
    "entry": (24, "Q"), "phoff": (32, "Q"), "shoff": (40, "Q"),
    "ehsize": (52, "H"), "phentsize": (54, "H"), "phnum": (56, "H"),
    "shentsize": (58, "H"), "shnum": (60, "H"), "shstrndx": (62, "H"),
}
PH_FIELDS = {
    "type": (0, "I"), "flags": (4, "I"), "offset": (8, "Q"),
    "vaddr": (16, "Q"), "paddr": (24, "Q"), "filesz": (32, "Q"),
    "memsz": (40, "Q"), "align": (48, "Q"),
}


def eh(blob, name, value):
    offset, fmt = EH_FIELDS[name]
    return mutate(blob, offset, fmt, value)


def ph(blob, name, value, index=0):
    offset, fmt = PH_FIELDS[name]
    return mutate(blob, EHDR.size + index * PHDR.size + offset, fmt, value)


def address(blob, value):
    return ph(ph(blob, "paddr", value), "vaddr", value)


def invalid_fixtures():
    """Named independent cases become separate unittest tests, not subtests."""
    good = elf()
    cases = {
        "empty": b"",
        "short_header": good[:63],
        "bad_magic": b"BAD!" + good[4:],
        "class32": mutate(good, 4, "B", 1),
        "big_endian": mutate(good, 5, "B", 2),
        "ident_version": mutate(good, 6, "B", 0),
        "machine_x86_64": eh(good, "machine", 62),
        "machine_none": eh(good, "machine", 0),
        "type_dyn": eh(good, "type", 3),
        "type_rel": eh(good, "type", 1),
        "type_none": eh(good, "type", 0),
        "header_version": eh(good, "version", 0),
        "header_too_small": eh(good, "ehsize", 63),
        "header_too_large": eh(good, "ehsize", 65),
        "phentsize_small": eh(good, "phentsize", 55),
        "phentsize_large": eh(good, "phentsize", 57),
        "no_phdrs": eh(good, "phnum", 0),
        "too_many_phdrs": eh(good, "phnum", 129),
        "extended_phnum": eh(good, "phnum", 0xffff),
        "phoff_zero": eh(good, "phoff", 0),
        "phoff_inside_header": eh(good, "phoff", 63),
        "phoff_past_end": eh(good, "phoff", len(good) + 1),
        "phoff_overflow": eh(good, "phoff", U64_MAX - 15),
        "truncated_phdr_table": good[:EHDR.size + PHDR.size - 1],
        "phdr_table_runs_off_end": eh(good, "phoff", len(good) - 55),
        "filesz_gt_memsz": ph(good, "memsz", 15),
        "file_data_truncated": good[:-1],
        "file_offset_past_end": ph(good, "offset", len(good) + 1),
        "file_offset_overflow": ph(good, "offset", U64_MAX - 7),
        "filesz_overflow": ph(ph(good, "memsz", U64_MAX), "filesz", U64_MAX),
        "vaddr_paddr_mismatch": ph(good, "vaddr", MRAM_BASE + 8),
        "below_mram": address(good, MRAM_BASE - 1),
        "past_mram": address(good, MRAM_BASE + MRAM_SIZE),
        "straddles_mram_end": address(good, MRAM_BASE + MRAM_SIZE - 47),
        "address_overflow": address(good, U64_MAX - 15),
        "memsz_overflow": ph(good, "memsz", U64_MAX),
        "align_non_power_two": ph(good, "align", 3),
        "align_congruence": ph(good, "align", 4096),
        "entry_odd": eh(good, "entry", MRAM_BASE + 1),
        "entry_outside": eh(good, "entry", MRAM_BASE - 2),
        "entry_at_file_end": eh(good, "entry", MRAM_BASE + 16),
        "entry_in_bss": eh(good, "entry", MRAM_BASE + 18),
        "entry_at_mem_end": eh(good, "entry", MRAM_BASE + 48),
        "entry_nonexec": ph(good, "flags", 6),
        "no_loads": ph(good, "type", 4),  # PT_NOTE
        "only_empty_load": elf([Segment(data=b"", memsz=0)]),
        "only_exec_bss": elf([Segment(data=b"", memsz=48)]),
        "dynamic_program_header": ph(good, "type", 2),
        "interpreter_program_header": ph(good, "type", 3),
        "tls_program_header": ph(good, "type", 7),
        "shoff_without_shnum": eh(good, "shoff", len(good)),
        "shnum_without_shoff": eh(good, "shnum", 1),
        "section_table_overflow": eh(eh(good, "shoff", U64_MAX - 31), "shnum", 1),
        "section_table_truncated": elf(sections=(0,))[:-1],
        "section_table_in_header": eh(elf(sections=(0,)), "shoff", 63),
        "section_entry_size": eh(elf(sections=(0,)), "shentsize", 63),
        "extended_section_numbering": eh(elf(sections=(0,)), "shnum", 0),
        "extended_names_without_sections": eh(good, "shstrndx", 0xffff),
        "reserved_section_count": eh(elf(sections=(0,)), "shnum", 0xff00),
        "names_without_sections": eh(good, "shstrndx", 1),
        "names_outside_sections": eh(elf(sections=(0,)), "shstrndx", 1),
        "relr_section": elf(sections=(0, 19)),
        "rel_section": elf(sections=(0, 9)),
        "rela_section": elf(sections=(0, 4)),
        "dynamic_section": elf(sections=(0, 6)),
        "overlap_file_data": elf([Segment(), Segment(addr=MRAM_BASE + 8)]),
        "overlap_bss_tail": elf([Segment(), Segment(addr=MRAM_BASE + 32)]),
        "overlap_contained": elf([Segment(memsz=256),
                                  Segment(addr=MRAM_BASE + 64)]),
        "overlap_reverse_order": elf([Segment(addr=MRAM_BASE + 32), Segment()]),
        "entry_data_segment": elf([Segment(), Segment(addr=MRAM_BASE + 0x2000,
                                                       flags=6)],
                                  entry=MRAM_BASE + 0x2000),
        "dynamic_after_valid_load": elf([Segment(), Segment(kind=2)]),
    }
    sections = elf(sections=(0, 1))
    offset = struct.unpack_from("<Q", sections, 40)[0] + SHDR.size
    cases["file_section_offset_overflow"] = mutate(mutate(sections, offset + 24, "Q", U64_MAX), offset + 32, "Q", 4096)
    cases["file_section_past_eof"] = mutate(mutate(sections, offset + 24, "Q", len(sections) - 1), offset + 32, "Q", 2)
    return cases


class LoaderTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="erbctl-tests-case-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.image = self.directory / "firmware.elf"
        self.missing_device = self.directory / "nonexistent-control"

    def native(self, data=None, args=("--check",), image=None):
        if data is not None:
            self.image.write_bytes(data)
        return subprocess.run(
            [str(NATIVE), "-d", str(self.missing_device), "load",
             str(image or self.image), *args], text=True, capture_output=True,
            timeout=5, cwd="/")

    def mock(self, data=None, args=(), mode="", repeat=False, command="load"):
        if data is not None:
            self.image.write_bytes(data)
        dump = self.directory / "mram.bin"
        env = dict(os.environ, ERBCTL_MOCK_MODE=mode, ERBCTL_MOCK_DUMP=str(dump))
        env.pop("ERBCTL_MOCK_REPEAT", None)
        if repeat:
            env["ERBCTL_MOCK_REPEAT"] = "1"
        argv = [str(MOCK), "-d", "/mock/control", command]
        if command == "load":
            argv += [str(self.image), "--mtd", "/mock/mtd"]
        result = subprocess.run(
            [*argv, *args],
            env=env, text=True, capture_output=True, timeout=5, cwd="/")
        events = [line.split()[1:] for line in result.stdout.splitlines()
                  if line.startswith("MOCK ")]
        final = [event for event in events if event[0] == "final"]
        self.assertEqual(len(final), 1, result.stdout + result.stderr)
        state = [int(x, 16) for x in final[0][1:5]]
        counts = [int(x) for x in final[0][5:]]
        self.assertEqual(counts[-1], 0, result.stdout + result.stderr)
        return result, events, state, counts, dump.read_bytes()

    def assert_valid(self, data, count=1):
        result = self.native(data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{count} LOAD segments", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertFalse(self.missing_device.exists())

    def assert_invalid(self, data):
        for args in (("--check",), (), ("--start", "--verify")):
            result = self.native(data, args)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("invalid ELF:", result.stderr)
            self.assertNotIn(str(self.missing_device), result.stderr)
            self.assertNotIn("ELF validated", result.stdout)
        result, events, state, counts, memory = self.mock(data, args=("--start",))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(counts[:2], [0, 0])
        self.assertEqual(state, [4, 0, 0, 0])
        self.assertEqual(memory, b"\xa5" * 65536)
        self.assertEqual([e[0] for e in events], ["run", "result", "final"])


class ParserTests(LoaderTestCase):
    def test_sectionless_exec(self):
        self.assert_valid(elf())

    def test_two_segments(self):
        self.assert_valid(elf([Segment(), Segment(addr=MRAM_BASE + 0x2000,
                                                  data=b"data", flags=6)]), 2)

    def test_zero_file_bss_segment(self):
        self.assert_valid(elf([Segment(), Segment(addr=MRAM_BASE + 0x2000,
                                                  data=b"", memsz=4097, flags=6)]), 2)

    def test_touching_not_overlapping_segments(self):
        self.assert_valid(elf([Segment(), Segment(addr=MRAM_BASE + 48)]), 2)

    def test_reverse_segment_order(self):
        self.assert_valid(elf([Segment(addr=MRAM_BASE + 0x2000), Segment()]), 2)

    def test_last_mram_bytes(self):
        addr = MRAM_BASE + MRAM_SIZE - 16
        self.assert_valid(elf([Segment(addr=addr, memsz=16)], entry=addr))

    def test_entry_two_byte_aligned_for_compressed_instructions(self):
        self.assert_valid(elf(entry=MRAM_BASE + 2))

    def test_alignment_zero(self):
        self.assert_valid(elf([Segment(align=0)]))

    def test_alignment_congruent(self):
        self.assert_valid(elf([Segment(offset=4096, align=4096)]))

    def test_nonrelocating_sections(self):
        self.assert_valid(elf(sections=(0, 1, 8)))  # NULL, PROGBITS, NOBITS

    def test_ignored_note_header(self):
        self.assert_valid(elf([Segment(), Segment(kind=4)]))

    def test_zero_memory_segment_ignored(self):
        self.assert_valid(elf([Segment(), Segment(data=b"", memsz=0)]))

    def test_maximum_program_headers(self):
        self.assert_valid(elf([Segment()] + [Segment(kind=4)] * 127))

    def test_sectionless_header_entrysize_unused(self):
        self.assert_valid(eh(elf(), "shentsize", 64))

    def test_missing_image_before_device_open(self):
        result = self.native(image=self.directory / "missing.elf")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing.elf", result.stderr)
        self.assertNotIn(str(self.missing_device), result.stderr)

    def test_directory_is_not_elf(self):
        result = self.native(image=self.directory)
        self.assertEqual(result.returncode, 1)
        self.assertIn("expected a regular ELF", result.stderr)

    def test_fifo_does_not_block_or_touch_device(self):
        fifo = self.directory / "fifo"
        os.mkfifo(fifo)
        result = self.native(image=fifo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("expected a regular ELF", result.stderr)

    def test_oversize_regular_file_rejected_without_read(self):
        with self.image.open("wb") as file:
            file.write(elf())
            file.truncate(64 * 1024 * 1024 + 1)  # sparse, no 64 MiB allocation
        result = self.native()
        self.assertEqual(result.returncode, 1)
        self.assertIn("between 64 bytes and 64 MiB", result.stderr)

    def test_valid_normal_load_reaches_missing_device(self):
        result = self.native(elf(), args=())
        self.assertEqual(result.returncode, 1)
        self.assertIn("ELF validated", result.stdout)
        self.assertIn(str(self.missing_device), result.stderr)


for _name, _data in invalid_fixtures().items():
    def _test(self, data=_data):
        self.assert_invalid(data)
    _test.__name__ = f"test_reject_{_name}"
    setattr(ParserTests, _test.__name__, _test)


class CommandLineTests(LoaderTestCase):
    def test_check_verify_mtd_no_device_access(self):
        self.assert_valid(elf())
        result, events, state, counts, _ = self.mock(
            elf(), args=("--check", "--verify"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(counts[:2], [0, 0])
        self.assertEqual(state, [4, 0, 0, 0])

    def test_invalid_options_before_even_reading_image(self):
        for args in (("--start", "--check"), ("--mtd",),
                     ("--unknown",), ("--no-verify",)):
            with self.subTest(args=args):
                result = self.native(args=args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage:", result.stderr)
                self.assertNotIn(str(self.image), result.stderr)
                self.assertNotIn(str(self.missing_device), result.stderr)

    def test_load_requires_image(self):
        result = subprocess.run(
            [str(NATIVE), "-d", str(self.missing_device), "load"],
            text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertNotIn(str(self.missing_device), result.stderr)

    def test_no_command_usage_without_device(self):
        result = subprocess.run([str(NATIVE)], text=True, capture_output=True,
                                timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)


def upload_image():
    return elf([Segment(), Segment(addr=MRAM_BASE + 0x2000,
                                  data=b"initialized-data!", memsz=8193, flags=6)])


class DeviceTests(LoaderTestCase):
    def assert_contents(self, memory):
        expected = bytearray(b"\xa5" * 65536)
        expected[:16] = CODE
        expected[16:48] = b"\0" * 32
        data = b"initialized-data!"
        expected[0x2000:0x2000 + len(data)] = data
        expected[0x2000 + len(data):0x4001] = b"\0" * (8193 - len(data))
        self.assertEqual(memory, expected, "LOAD data/BSS or untouched gaps differ")

    def assert_sequence(self, events, start):
        writes = [i for i, e in enumerate(events) if e[0] == "write"]
        reads = [i for i, e in enumerate(events) if e[0] == "read"]
        boot = [i for i, e in enumerate(events)
                if e[:2] == ["regwrite", "80d00018"]]
        self.assertTrue(writes and reads)
        self.assertEqual(len(boot), 1)
        self.assertLess(max(writes), min(reads), "verification must be a separate pass")
        self.assertLess(max(reads), boot[0], "boot must be programmed only after verify")
        first = writes[0]
        for event in (["regwrite", "40000028", "6"],
                      ["regread", "40000028", "6"],
                      ["regwrite", "80f40240", "ff"],
                      ["regread", "80f40240", "ff"],
                      ["regwrite", "80f40010", "ff"],
                      ["regread", "80f40010", "ff"]):
            self.assertIn(event, events[:first])
        self.assertLess(events.index(["regwrite", "40000028", "6"]),
                        events.index(["regwrite", "80f40240", "ff"]))
        for event in (["flock", "control", "6"], ["flock", "mtd", "6"]):
            self.assertIn(event, events[:first])
        releases = [i for i, e in enumerate(events)
                    if e == ["regwrite", "40000028", "4"]]
        enables = [i for i, e in enumerate(events)
                   if e == ["regwrite", "80f40240", "fe"]]
        if start:
            self.assertEqual(len(enables), 1)
            self.assertEqual(len(releases), 1)
            self.assertLess(boot[0], enables[0])
            self.assertLess(enables[0], releases[0])
            self.assertLess(events.index(["regread", "80d00018", "40000000"]),
                            enables[0])
            self.assertIn(["regwrite", "80f40010", "ff"],
                          events[enables[0]:releases[0]])
        else:
            self.assertEqual(releases, [])
            self.assertEqual(enables, [])

    def assert_chunk_plan(self, events):
        # Both initialized data and zero-fill must be written AND read back,
        # including the last sub-CHUNK tail of the second segment.
        plan = [[hex(offset)[2:], str(length)] for offset, length in
                ((0, 16), (16, 32), (0x2000, 17), (0x2011, 4096), (0x3011, 4080))]
        for operation in ("write", "read"):
            self.assertEqual([e[1:] for e in events if e[0] == operation], plan)

    def test_default_verifies_data_and_bss_leaves_held(self):
        result, events, state, counts, memory = self.mock(upload_image())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state, [6, 0xff, 0xff, MRAM_BASE])
        self.assertEqual(counts[:2], [1, 1])
        self.assert_contents(memory)
        self.assert_sequence(events, start=False)
        self.assert_chunk_plan(events)
        self.assertIn("CPU remains held", result.stdout)

    def test_start_only_after_full_verification(self):
        result, events, state, _, memory = self.mock(upload_image(), args=("--start",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state, [4, 0xfe, 0xff, MRAM_BASE])
        self.assert_contents(memory)
        self.assert_sequence(events, start=True)
        self.assert_chunk_plan(events)

    def test_explicit_verify_is_same_as_mandatory_verification(self):
        result, events, state, _, memory = self.mock(upload_image(), args=("--verify",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_contents(memory)
        self.assert_sequence(events, start=False)

    def test_reload_zeroes_dirty_bss_again(self):
        result, events, state, counts, memory = self.mock(
            upload_image(), repeat=True, args=("--start",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state, [4, 0xfe, 0xff, MRAM_BASE])
        self.assertEqual(counts[:2], [2, 2])
        runs = [i for i, e in enumerate(events) if e[0] == "run"]
        self.assertEqual(len(runs), 2)
        self.assert_sequence(events[runs[0]:runs[1]], start=True)
        self.assert_sequence(events[runs[1]:], start=True)
        self.assert_contents(memory)

    def test_pure_bss_segment_filled_and_verified(self):
        image = elf([Segment(), Segment(addr=MRAM_BASE + 0x2000, data=b"",
                                        memsz=8193, flags=6)])
        result, events, state, _, memory = self.mock(image, args=("--start",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(memory[0x2000:0x4001], b"\0" * 8193)
        self.assert_sequence(events, start=True)

    def test_initialized_data_larger_than_chunk(self):
        data = bytes(range(256)) * 33 + b"last-byte"
        result, events, state, _, memory = self.mock(
            elf([Segment(data=data, memsz=len(data) + 4097)]), args=("--start",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state, [4, 0xfe, 0xff, MRAM_BASE])
        self.assertEqual(memory[:len(data)], data)
        self.assertEqual(memory[len(data):len(data) + 4097], b"\0" * 4097)
        self.assertEqual(memory[len(data) + 4097:], b"\xa5" * (65536 - len(data) - 4097))
        self.assert_sequence(events, start=True)

    def test_failed_cleanup_hold_is_reported_not_claimed_safe(self):
        result, events, state, _, memory = self.mock(
            upload_image(), args=("--start",), mode="hold_write_persistent")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: failed to confirm CPU hold", result.stderr)
        self.assertNotIn("CPU remains held", result.stderr)
        self.assertEqual(state, [4, 0xff, 0xff, 0])
        self.assertEqual(memory, b"\xa5" * 65536)
        self.assertEqual(events.count(["regwrite", "40000028", "6"]), 2)
        self.assertFalse(any(e[0] in ("read", "write") for e in events))

    def test_cleanup_disables_threads_even_if_hold_register_fails_after_release(self):
        result, events, state, _, _ = self.mock(
            upload_image(), args=("--start",), mode="release_cleanup_hold_persistent")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(state[:3], [4, 0xff, 0xff])
        self.assertIn("failed to confirm CPU hold", result.stderr)
        self.assertNotIn("CPU released:", result.stdout)
        self.assertEqual(events[-4:-2], [["close", "mtd"], ["close", "control"]])

    def test_late_cancellation_reholds_and_reports_failure(self):
        for mode in ("signal_boot", "signal_enable", "signal_release_write", "signal_release_read"):
            with self.subTest(mode=mode):
                result, events, state, _, _ = self.mock(
                    upload_image(), args=("--start",), mode=mode)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(state[:3], [6, 0xff, 0xff])
                self.assertIn("CPU remains held", result.stderr)
                self.assertNotIn("CPU released:", result.stdout)

    def test_hold_command_preserves_entire_mram(self):
        result, events, state, counts, memory = self.mock(command="hold")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state, [6, 0xff, 0xff, 0])
        self.assertEqual(counts[:2], [1, 0])
        self.assertEqual(memory, b"\xa5" * 65536)
        self.assertFalse(any(e[0] in ("read", "write") for e in events))

    def test_hold_command_reports_register_error(self):
        result, events, state, _, memory = self.mock(
            command="hold", mode="hold_read_mismatch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("CPU hold failed", result.stderr)
        self.assertEqual(memory, b"\xa5" * 65536)

    def assert_preflight_failure(self, mode):
        result, events, state, _, memory = self.mock(
            upload_image(), args=("--start",), mode=mode)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(result.stderr)
        self.assertEqual(state, [0 if mode == "reset_bad" else 4, 0, 0, 0])
        self.assertFalse(any(e[0] in ("regwrite", "read", "write") for e in events),
                         result.stdout)
        self.assertEqual(memory, b"\xa5" * 65536)

    def assert_upload_failure(self, mode):
        result, events, state, _, memory = self.mock(
            upload_image(), args=("--start",), mode=mode)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(state[:3], [6, 0xff, 0xff], result.stdout)
        self.assertIn("CPU remains held", result.stderr)
        if mode not in ("boot_write_error", "boot_read_mismatch",
                        "enable_error", "release_write_error", "release_read_mismatch"):
            self.assertFalse(any(e[:2] == ["regwrite", "80d00018"] for e in events))
        # Events include attempted writes; a failed release attempt is allowed
        # only in the explicit release-fault tests, after successful verification.
        if mode not in ("release_write_error", "release_read_mismatch"):
            self.assertNotIn(["regwrite", "40000028", "4"], events)
        if mode.startswith("corrupt"):
            self.assertIn("verification mismatch", result.stderr)
        if mode in ("write_error", "write_zero", "read_error", "read_zero"):
            self.assertIn("Input/output error", result.stderr)
        if mode in ("signal_read", "signal_write"):
            self.assertIn("Interrupted system call", result.stderr)
        if mode == "short_then_error":
            self.assertEqual(memory[:7], CODE[:7])
            self.assertEqual(memory[7:], b"\xa5" * (65536 - 7))
        self.assertIn(["close", "mtd"], events)
        self.assertIn(["close", "control"], events)

    def assert_recoverable_io(self, mode):
        result, events, state, _, memory = self.mock(
            upload_image(), args=("--start",), mode=mode)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state, [4, 0xfe, 0xff, MRAM_BASE])
        self.assert_contents(memory)
        self.assert_sequence(events, start=True)


PREFLIGHT_FAULTS = (
    "file_read_error", "file_read_zero", "control_open", "control_lock", "info_error",
    "mram_zero", "mram_large", "mram_small", "mtd_open", "mtd_info_error",
    "mtd_type", "mtd_size", "fstat_error", "not_character", "sysfs_error",
    "parent_mismatch", "mtd_lock", "reset_read_error", "reset_bad",
)
UPLOAD_FAULTS = (
    "hold_write_error", "hold_read_mismatch", "disable0_error", "disable1_error",
    "write_error", "write_zero", "write_after_data", "short_then_error",
    "read_error", "read_zero", "read_after_data", "corrupt_data", "corrupt_bss",
    "corrupt_second_data", "corrupt_late_bss",
    "boot_write_error", "boot_read_mismatch", "enable_error",
    "release_write_error", "release_read_mismatch", "signal_write", "signal_read",
)
RECOVERABLE_IO = (
    "short_io", "short_file", "write_eintr", "read_eintr", "file_read_eintr",
    "already_held",
)
for _group, _modes, _assertion in (
    ("preflight", PREFLIGHT_FAULTS, "assert_preflight_failure"),
    ("failure", UPLOAD_FAULTS, "assert_upload_failure"),
    ("recover", RECOVERABLE_IO, "assert_recoverable_io"),
):
    for _mode in _modes:
        def _test(self, mode=_mode, assertion=_assertion):
            getattr(self, assertion)(mode)
        _test.__name__ = f"test_{_group}_{_mode}"
        setattr(DeviceTests, _test.__name__, _test)


if __name__ == "__main__":
    unittest.main()
