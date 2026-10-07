// SPDX-License-Identifier: GPL-2.0
/* Strict, MRAM-only ELF loader. All ELF validation precedes device access.
 * Device locks are advisory: raw MTD/ioctl users must be quiesced externally.
 */
#define _FILE_OFFSET_BITS 64
#include <elf.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mtd/mtd-user.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>
#include "erbium-xspi.h"
#include "erbium-loader.h"

#define MRAM_BASE UINT64_C(0x40000000)
#define MRAM_SIZE UINT64_C(0x01000000)
#define MAX_ELF_SIZE (64U * 1024 * 1024)
#define MAX_SEGMENTS 128
#define CHUNK 4096
#define SOFT_RESET UINT64_C(0x40000028) /* xSPI address, not CPU address */
#define MINION_BOOT UINT64_C(0x80d00018)
#define THREAD0_DISABLE UINT64_C(0x80f40240)
#define THREAD1_DISABLE UINT64_C(0x80f40010)
#define HOLD 6U    /* warm reset asserted, mram_rst_b deasserted */
#define RELEASE 4U

struct segment {
	uint64_t offset, addr, filesz, memsz;
	uint32_t flags;
};
struct image {
	uint8_t *bytes;
	size_t size, count;
	uint64_t entry;
	struct segment seg[MAX_SEGMENTS];
};
static volatile sig_atomic_t interrupted;
static void on_signal(int sig) { interrupted = sig; }

static int invalid(const char *why)
{
	fprintf(stderr, "erbctl: invalid ELF: %s\n", why);
	return -1;
}
static bool range(uint64_t start, uint64_t len, uint64_t size)
{
	return start <= size && len <= size - start;
}
static int exact_read(int fd, void *buf, size_t len, off_t offset)
{
	uint8_t *p = buf;
	while (len) {
		ssize_t n = pread(fd, p, len, offset);
		if (n < 0 && errno == EINTR && !interrupted)
			continue;
		if (n <= 0 || interrupted) {
			if (n == 0) errno = EIO;
			if (interrupted) errno = EINTR;
			return -1;
		}
		p += n; len -= n; offset += n;
	}
	return 0;
}
static int exact_write(int fd, const void *buf, size_t len, off_t offset)
{
	const uint8_t *p = buf;
	while (len) {
		if (interrupted) { errno = EINTR; return -1; }
		ssize_t n = pwrite(fd, p, len, offset);
		if (n < 0 && errno == EINTR && !interrupted)
			continue;
		if (n <= 0) { if (!n) errno = EIO; return -1; }
		p += n; len -= n; offset += n;
	}
	return 0;
}

static int parse_image(struct image *img, const char *path)
{
	Elf64_Ehdr eh;
	struct stat st;
	int fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
	if (fd < 0) { perror(path); return -1; }
	if (fstat(fd, &st) || !S_ISREG(st.st_mode) ||
	    st.st_size < (off_t)sizeof(eh) || st.st_size > MAX_ELF_SIZE) {
		close(fd);
		return invalid("expected a regular ELF file between 64 bytes and 64 MiB");
	}
	img->size = st.st_size;
	img->bytes = malloc(img->size);
	if (!img->bytes) { close(fd); perror("ELF allocation"); return -1; }
	int ret = exact_read(fd, img->bytes, img->size, 0);
	close(fd);
	if (ret) { perror("read ELF"); return -1; }
	/* Snapshot bytes, not mmap: later file modifications cannot alter our plan. */
	memcpy(&eh, img->bytes, sizeof(eh));
	if (memcmp(eh.e_ident, ELFMAG, SELFMAG) ||
	    eh.e_ident[EI_CLASS] != ELFCLASS64 || eh.e_ident[EI_DATA] != ELFDATA2LSB ||
	    eh.e_ident[EI_VERSION] != EV_CURRENT || le32toh(eh.e_version) != EV_CURRENT ||
	    le16toh(eh.e_machine) != EM_RISCV || le16toh(eh.e_type) != ET_EXEC ||
	    le16toh(eh.e_ehsize) != sizeof(eh))
		return invalid("requires ELF64 little-endian RISC-V ET_EXEC");
	uint16_t nph = le16toh(eh.e_phnum);
	uint64_t phoff = le64toh(eh.e_phoff);
	if (!nph || nph > MAX_SEGMENTS || le16toh(eh.e_phentsize) != sizeof(Elf64_Phdr) ||
	    phoff < sizeof(eh) || !range(phoff, (uint64_t)nph * sizeof(Elf64_Phdr), img->size))
		return invalid("invalid or truncated program header table");
	img->entry = le64toh(eh.e_entry);
	bool entry_ok = false;
	for (unsigned i = 0; i < nph; i++) {
		Elf64_Phdr ph;
		memcpy(&ph, img->bytes + phoff + i * sizeof(ph), sizeof(ph));
		uint32_t type = le32toh(ph.p_type);
		if (type == PT_DYNAMIC || type == PT_INTERP || type == PT_TLS)
			return invalid("dynamic linking and TLS are not supported");
		if (type != PT_LOAD) continue;
		struct segment seg = { le64toh(ph.p_offset), le64toh(ph.p_paddr),
			le64toh(ph.p_filesz), le64toh(ph.p_memsz), le32toh(ph.p_flags) };
		uint64_t align = le64toh(ph.p_align);
		if (seg.addr != le64toh(ph.p_vaddr))
			return invalid("physical and virtual addresses must match");
		if (seg.filesz > seg.memsz || !range(seg.offset, seg.filesz, img->size))
			return invalid("invalid or truncated LOAD data");
		if (seg.addr < MRAM_BASE || !range(seg.addr - MRAM_BASE, seg.memsz, MRAM_SIZE))
			return invalid("LOAD segment outside 16 MiB MRAM");
		if (align > 1 && ((align & (align - 1)) || seg.addr % align != seg.offset % align))
			return invalid("invalid LOAD alignment");
		if (!seg.memsz) continue;
		for (size_t j = 0; j < img->count; j++) {
			struct segment *s = &img->seg[j];
			if (seg.addr < s->addr + s->memsz && s->addr < seg.addr + seg.memsz)
				return invalid("overlapping LOAD segments");
		}
		if ((seg.flags & PF_X) && img->entry >= seg.addr &&
		    img->entry - seg.addr < seg.filesz && !(img->entry & 1))
			entry_ok = true;
		img->seg[img->count++] = seg;
	}
	if (!img->count || !entry_ok)
		return invalid("entry must be aligned and inside file-backed executable LOAD data");
	/* Sections are optional, but reject relocations and malformed section tables
	 * when present. Extended ELF numbering is deliberately unsupported. */
	uint64_t shoff = le64toh(eh.e_shoff);
	uint16_t nsh = le16toh(eh.e_shnum);
	uint16_t names = le16toh(eh.e_shstrndx);
	if (nsh >= SHN_LORESERVE || names >= SHN_LORESERVE ||
	    (names != SHN_UNDEF && names >= nsh))
		return invalid("invalid or unsupported section numbering");
	if (shoff || nsh) {
		if (!nsh || shoff < sizeof(eh) || le16toh(eh.e_shentsize) != sizeof(Elf64_Shdr) ||
		    !range(shoff, (uint64_t)nsh * sizeof(Elf64_Shdr), img->size))
			return invalid("invalid or unsupported section header table");
		for (unsigned i = 0; i < nsh; i++) {
			Elf64_Shdr sh;
			memcpy(&sh, img->bytes + shoff + i * sizeof(sh), sizeof(sh));
			uint32_t type = le32toh(sh.sh_type);
			if (type != SHT_NOBITS && !range(le64toh(sh.sh_offset), le64toh(sh.sh_size), img->size))
				return invalid("file-backed section outside ELF file");
			if (type == SHT_REL || type == SHT_RELA || type == SHT_RELR || type == SHT_DYNAMIC)
				return invalid("relocations/dynamic sections are not supported");
		}
	}
	printf("ELF validated: %zu LOAD segments, entry 0x%llx\n", img->count,
	       (unsigned long long)img->entry);
	return 0;
}

static int reg_read(int fd, uint64_t addr, uint64_t *value)
{
	uint64_t wire = 0;
	struct erbium_mem_xfer x = { .addr = addr, .buf = (uintptr_t)&wire, .len = 8 };
	if (ioctl(fd, ERBIUM_IOC_MEM_READ, &x)) return -1;
	*value = le64toh(wire);
	return 0;
}
static int reg_write(int fd, uint64_t addr, uint64_t value)
{
	uint64_t wire = htole64(value);
	struct erbium_mem_xfer x = { .addr = addr, .buf = (uintptr_t)&wire, .len = 8 };
	return ioctl(fd, ERBIUM_IOC_MEM_WRITE, &x);
}
static int reg_set(int fd, uint64_t addr, uint64_t value)
{
	uint64_t got;
	if (reg_write(fd, addr, value) || reg_read(fd, addr, &got)) return -1;
	if (got != value) {
		fprintf(stderr, "erbctl: register 0x%llx readback 0x%llx, expected 0x%llx\n",
			(unsigned long long)addr, (unsigned long long)got, (unsigned long long)value);
		errno = EIO;
		return -1;
	}
	return 0;
}
static int hold_cpu(int fd)
{
	int error = 0;
	/* Independent attempts: even if the reset register fails, disable both
	 * thread groups as a secondary stop mechanism. Never claim full hold
	 * confirmation unless every readback succeeded. */
	const uint64_t addr[] = { SOFT_RESET, THREAD0_DISABLE, THREAD1_DISABLE };
	const uint64_t value[] = { HOLD, 0xff, 0xff };
	for (size_t i = 0; i < 3; i++) {
		if (reg_set(fd, addr[i], value[i])) {
			int e = errno;
			fprintf(stderr, "erbctl: hold register 0x%llx failed: %s\n",
				(unsigned long long)addr[i], strerror(e));
			if (!error) error = e;
		}
		/* Conservative settling delay; readback is not a CPU-stopped ack. */
		if (i == 0) usleep(1000);
	}
	if (error) { errno = error; return -1; }
	return 0;
}
static int open_control(const char *device)
{
	int fd = open(device, O_RDWR | O_CLOEXEC);
	if (fd < 0) { perror(device); return -1; }
	if (flock(fd, LOCK_EX | LOCK_NB)) {
		perror("erbctl: device busy (another cooperating erbctl process?)");
		close(fd); return -1;
	}
	return fd;
}
static int same_parent(int ctl, int mtd)
{
	struct stat cs, ms;
	char path[128], cparent[PATH_MAX], mparent[PATH_MAX];
	if (fstat(ctl, &cs) || fstat(mtd, &ms)) return -1;
	if (!S_ISCHR(cs.st_mode) || !S_ISCHR(ms.st_mode)) { errno = ENODEV; return -1; }
	snprintf(path, sizeof(path), "/sys/dev/char/%u:%u/device", major(cs.st_rdev), minor(cs.st_rdev));
	if (!realpath(path, cparent)) return -1;
	snprintf(path, sizeof(path), "/sys/dev/char/%u:%u/device", major(ms.st_rdev), minor(ms.st_rdev));
	if (!realpath(path, mparent)) return -1;
	if (strcmp(cparent, mparent)) { errno = EXDEV; return -1; }
	return 0;
}

int erbctl_hold(const char *device)
{
	int fd = open_control(device);
	if (fd < 0) return 1;
	int ret = hold_cpu(fd);
	if (ret) perror("erbctl: CPU hold failed");
	else puts("CPU held in warm reset; all threads disabled (MRAM preserved)");
	close(fd);
	return ret ? 1 : 0;
}

int erbctl_load(const char *device, int argc, char **argv)
{
	const char *mtdpath = "/dev/mtd0";
	bool start = false, check = false, touched = false;
	int fd = -1, mtd = -1, rc = 1;
	struct image img = {0};
	struct erbium_info info;
	struct mtd_info_user mi;
	struct sigaction oldint, oldterm, sa = { .sa_handler = on_signal };
	bool signals = false, masked = false;
	sigset_t block, oldmask, pending;
	interrupted = 0;
	if (!argc) goto usage;
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--start")) start = true;
		else if (!strcmp(argv[i], "--check")) check = true;
		else if (!strcmp(argv[i], "--verify")) { /* always verified */ }
		else if (!strcmp(argv[i], "--mtd") && i + 1 < argc) mtdpath = argv[++i];
		else goto usage;
	}
	if (check && start) goto usage;
	if (parse_image(&img, argv[0])) goto out;
	if (check) { rc = 0; goto out; }
	fd = open_control(device);
	if (fd < 0) goto out;
	if (ioctl(fd, ERBIUM_IOC_GET_INFO, &info)) { perror("GET_INFO"); goto out; }
	if (!info.mram_size || info.mram_size > MRAM_SIZE) {
		fprintf(stderr, "erbctl: unsupported MRAM size\n"); goto out;
	}
	for (size_t i = 0; i < img.count; i++)
		if (!range(img.seg[i].addr - MRAM_BASE, img.seg[i].memsz, info.mram_size)) {
			fprintf(stderr, "erbctl: image exceeds detected MRAM\n"); goto out;
		}
	mtd = open(mtdpath, O_RDWR | O_CLOEXEC);
	if (mtd < 0) { perror(mtdpath); goto out; }
	if (ioctl(mtd, MEMGETINFO, &mi)) { perror("MTD MEMGETINFO"); goto out; }
	if (mi.type != MTD_RAM || mi.size != info.mram_size) {
		fprintf(stderr, "erbctl: MTD must be matching Erbium MTD_RAM\n"); goto out;
	}
	if (same_parent(fd, mtd)) { perror("erbctl: control/MTD device pairing (mount sysfs)"); goto out; }
	if (flock(mtd, LOCK_EX | LOCK_NB)) { perror("erbctl: MTD busy"); goto out; }
	uint64_t reset;
	if (reg_read(fd, SOFT_RESET, &reset)) { perror("SoftReset read"); goto out; }
	if (reset != RELEASE && reset != HOLD) {
		fprintf(stderr, "erbctl: unexpected SoftReset 0x%llx; need active MRAM and live backend\n",
			(unsigned long long)reset); goto out;
	}
	/* Handle normal cancellation without releasing a partially loaded image.
	 * SIGKILL/power loss cannot be handled; hardware hold was asserted first. */
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGINT, &sa, &oldint)) { perror("sigaction"); goto out; }
	if (sigaction(SIGTERM, &sa, &oldterm)) {
		sigaction(SIGINT, &oldint, NULL); perror("sigaction"); goto out;
	}
	signals = true;
	touched = true;
	if (hold_cpu(fd)) { perror("CPU hold"); goto out; }
	puts("CPU held; uploading MRAM segments");
	uint8_t zeros[CHUNK] = {0}, got[CHUNK];
	for (size_t i = 0; i < img.count; i++) {
		const struct segment *s = &img.seg[i];
		uint64_t base = s->addr - MRAM_BASE;
		for (uint64_t off = 0; off < s->memsz;) {
			bool data = off < s->filesz;
			uint64_t remain = (data ? s->filesz : s->memsz) - off;
			size_t n = remain < CHUNK ? remain : CHUNK;
			const uint8_t *want = data ? img.bytes + s->offset + off : zeros;
			if (exact_write(mtd, want, n, base + off)) { perror("MRAM write"); goto out; }
			off += n;
		}
	}
	/* Separate verification pass, including zero-fill, before any hart release. */
	for (size_t i = 0; i < img.count; i++) {
		const struct segment *s = &img.seg[i];
		uint64_t base = s->addr - MRAM_BASE;
		for (uint64_t off = 0; off < s->memsz;) {
			bool data = off < s->filesz;
			uint64_t remain = (data ? s->filesz : s->memsz) - off;
			size_t n = remain < CHUNK ? remain : CHUNK;
			const uint8_t *want = data ? img.bytes + s->offset + off : zeros;
			if (exact_read(mtd, got, n, base + off)) { perror("MRAM readback"); goto out; }
			if (memcmp(want, got, n)) {
				fprintf(stderr, "erbctl: verification mismatch at MRAM offset 0x%llx\n",
					(unsigned long long)(base + off)); goto out;
			}
			off += n;
		}
	}
	if (interrupted) { errno = EINTR; perror("load interrupted"); goto out; }
	if (reg_set(fd, MINION_BOOT, img.entry)) { perror("MINION_BOOT"); goto out; }
	puts("MRAM verified (including zero-fill); boot PC programmed");
	if (start) {
		if (reg_set(fd, THREAD0_DISABLE, 0xfe) || reg_set(fd, THREAD1_DISABLE, 0xff) ||
		    interrupted || reg_set(fd, SOFT_RESET, RELEASE)) {
			perror("CPU release"); goto out;
		}
	}
	/* Successful-completion boundary: catch cancellation during the release
	 * write/readback too. Once this blocked check commits success, later
	 * signals may terminate the tool but do not undo the completed boot. */
	sigemptyset(&block);
	sigaddset(&block, SIGINT); sigaddset(&block, SIGTERM);
	if (sigprocmask(SIG_BLOCK, &block, &oldmask)) { perror("sigprocmask"); goto out; }
	masked = true;
	if (sigpending(&pending)) { perror("sigpending"); goto out; }
	if (interrupted || sigismember(&pending, SIGINT) || sigismember(&pending, SIGTERM)) {
		errno = EINTR; perror("load interrupted"); goto out;
	}
	rc = 0;
	if (start) puts("CPU released: minion 0/thread 0 (execution readiness is not checked)");
	else puts("CPU remains held; use load --start to reload, verify and release");
out:
	if (rc && touched) {
		if (hold_cpu(fd)) fprintf(stderr, "erbctl: ERROR: failed to confirm CPU hold after load failure\n");
		else fprintf(stderr, "erbctl: load failed; CPU remains held, no image started\n");
	}
	if (signals) { sigaction(SIGINT, &oldint, NULL); sigaction(SIGTERM, &oldterm, NULL); }
	if (mtd >= 0) close(mtd);
	if (fd >= 0) close(fd);
	free(img.bytes);
	if (masked) sigprocmask(SIG_SETMASK, &oldmask, NULL);
	return rc;
usage:
	fprintf(stderr, "usage: erbctl [-d control] load ELF [--mtd /dev/mtd0] [--verify] [--start | --check]\n"
		"  Verification is mandatory. Default: load, verify and leave CPU held.\n"
		"  --check validates the ELF without opening any device.\n");
	return 2;
}
