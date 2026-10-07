// SPDX-License-Identifier: GPL-2.0
/*
 * Native, process-local device model for test_erbctl_loader.py.
 *
 * Include the unmodified production sources, replacing only their syscall
 * call sites. Include libc headers BEFORE defining the macros: this avoids
 * both prototype pollution and glibc's _FILE_OFFSET_BITS/fstat64 aliases.
 * Image files still use real libc I/O; no /dev node or sysfs file is touched.
 * Faults are one-shot so failure cleanup can confirm a hardware hold, except
 * the explicit persistent-hold failure used to test the critical diagnostic.
 */
#define _FILE_OFFSET_BITS 64
#include <elf.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mtd/mtd-user.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>
#include "../linux/tools/erbium-xspi.h"

#define CTL_FD 9001
#define MTD_FD 9002
#define RAM_SIZE 0x01000000
#define RESET_REG UINT64_C(0x40000028)
#define BOOT_REG UINT64_C(0x80d00018)
#define T0_REG UINT64_C(0x80f40240)
#define T1_REG UINT64_C(0x80f40010)

static uint8_t ram[RAM_SIZE];
static uint64_t reset_value = 4, boot_value, t0_value, t1_value;
static const char *mode;
static bool fault_used;
static bool release_written;
static int control_opens, mtd_opens, violations;

static bool is_mode(const char *name) { return !strcmp(mode, name); }
static bool fault(const char *name)
{
	if (is_mode(name) && (!fault_used || strstr(name, "persistent"))) {
		fault_used = true;
		printf("MOCK fault %s\n", name);
		errno = EIO;
		return true;
	}
	return false;
}
static void violation(const char *why)
{
	violations++;
	printf("MOCK violation %s\n", why);
}
static int mock_open(const char *path, int flags, ...)
{
	if (!strcmp(path, "/mock/control")) {
		control_opens++;
		puts("MOCK open control");
		return fault("control_open") ? -1 : CTL_FD;
	}
	if (!strcmp(path, "/mock/mtd")) {
		mtd_opens++;
		puts("MOCK open mtd");
		return fault("mtd_open") ? -1 : MTD_FD;
	}
	/* All production non-device opens are read-only ELF files. */
	if (flags & (O_CREAT | O_WRONLY | O_RDWR)) {
		violation("unexpected file write");
		errno = EACCES;
		return -1;
	}
	return open(path, flags);
}
static int mock_close(int fd)
{
	if (fd == CTL_FD || fd == MTD_FD) {
		printf("MOCK close %s\n", fd == CTL_FD ? "control" : "mtd");
		return 0;
	}
	return close(fd);
}
static int mock_fstat(int fd, struct stat *st)
{
	if (fd != CTL_FD && fd != MTD_FD) return fstat(fd, st);
	printf("MOCK fstat %s\n", fd == CTL_FD ? "control" : "mtd");
	if (fault("fstat_error")) return -1;
	memset(st, 0, sizeof(*st));
	st->st_mode = is_mode("not_character") ? S_IFREG : S_IFCHR;
	st->st_rdev = makedev(fd == CTL_FD ? 240 : 90, 0);
	return 0;
}
static char *mock_realpath(const char *path, char *resolved)
{
	printf("MOCK realpath %s\n", path);
	if (fault("sysfs_error")) return NULL;
	if (strcmp(path, "/sys/dev/char/240:0/device") &&
	    strcmp(path, "/sys/dev/char/90:0/device")) {
		violation("unexpected sysfs path");
		errno = ENOENT;
		return NULL;
	}
	strcpy(resolved, is_mode("parent_mismatch") && strstr(path, "/90:")
	       ? "/sys/devices/other" : "/sys/devices/erbium");
	return resolved;
}
static int mock_flock(int fd, int operation)
{
	printf("MOCK flock %s %d\n", fd == CTL_FD ? "control" : "mtd", operation);
	if (fd != CTL_FD && fd != MTD_FD) violation("unexpected lock fd");
	if (operation != (LOCK_EX | LOCK_NB)) violation("nonexclusive lock");
	if (fault(fd == CTL_FD ? "control_lock" : "mtd_lock")) {
		errno = EWOULDBLOCK;
		return -1;
	}
	return 0;
}
static int mock_usleep(useconds_t usec) { (void)usec; return 0; }

static uint64_t *register_value(uint64_t addr)
{
	switch (addr) {
	case RESET_REG: return &reset_value;
	case BOOT_REG: return &boot_value;
	case T0_REG: return &t0_value;
	case T1_REG: return &t1_value;
	default: violation("unknown register"); return NULL;
	}
}
static int mock_ioctl(int fd, unsigned long request, ...)
{
	va_list ap;
	va_start(ap, request);
	void *arg = va_arg(ap, void *);
	va_end(ap);
	if (fd == CTL_FD && request == ERBIUM_IOC_GET_INFO) {
		puts("MOCK info");
		if (fault("info_error")) return -1;
		struct erbium_info *info = arg;
		memset(info, 0, sizeof(*info));
		info->mram_size = is_mode("mram_zero") ? 0 :
			is_mode("mram_small") ? 16 :
			is_mode("mram_large") ? RAM_SIZE + 1 : RAM_SIZE;
		return 0;
	}
	if (fd == MTD_FD && request == MEMGETINFO) {
		puts("MOCK mtdinfo");
		if (fault("mtd_info_error")) return -1;
		struct mtd_info_user *mi = arg;
		memset(mi, 0, sizeof(*mi));
		mi->type = is_mode("mtd_type") ? MTD_NORFLASH : MTD_RAM;
		mi->size = is_mode("mtd_size") ? RAM_SIZE / 2 : RAM_SIZE;
		return 0;
	}
	if (fd != CTL_FD || (request != ERBIUM_IOC_MEM_READ &&
			    request != ERBIUM_IOC_MEM_WRITE)) {
		violation("unknown ioctl");
		errno = ENOTTY;
		return -1;
	}
	struct erbium_mem_xfer *x = arg;
	uint64_t *reg = register_value(x->addr);
	if (!reg || x->len != 8 || x->flags) {
		violation("invalid register transfer");
		errno = EINVAL;
		return -1;
	}
	uint64_t *wire = (void *)(uintptr_t)x->buf;
	if (request == ERBIUM_IOC_MEM_READ) {
		printf("MOCK regread %llx %llx\n", (unsigned long long)x->addr,
		       (unsigned long long)*reg);
		if (x->addr == RESET_REG && fault("reset_read_error")) return -1;
		uint64_t value = *reg;
		if ((x->addr == RESET_REG && value == 6 && fault("hold_read_mismatch")) ||
		    (x->addr == BOOT_REG && fault("boot_read_mismatch")) ||
		    (x->addr == RESET_REG && value == 4 && release_written &&
		     fault("release_read_mismatch")))
			value ^= 1;
		*wire = htole64(value);
		if (x->addr == RESET_REG && release_written && fault("signal_release_read")) raise(SIGTERM);
		if (x->addr == RESET_REG && release_written && is_mode("release_cleanup_hold_persistent"))
			*wire ^= 1;
		return 0;
	}
	uint64_t value = le64toh(*wire);
	printf("MOCK regwrite %llx %llx\n", (unsigned long long)x->addr,
	       (unsigned long long)value);
	if ((x->addr == RESET_REG && value == 6 && fault("hold_write_error")) ||
	    (x->addr == RESET_REG && value == 6 && fault("hold_write_persistent")) ||
	    (x->addr == T0_REG && value == 0xff && fault("disable0_error")) ||
	    (x->addr == T1_REG && value == 0xff && fault("disable1_error")) ||
	    (x->addr == BOOT_REG && fault("boot_write_error")) ||
	    (x->addr == T0_REG && value == 0xfe && fault("enable_error")) ||
	    (x->addr == RESET_REG && value == 4 && fault("release_write_error")))
		return -1;
	if (x->addr == RESET_REG && value == 6 && release_written &&
	    fault("release_cleanup_hold_persistent")) return -1;
	*reg = value;
	if (x->addr == BOOT_REG && fault("signal_boot")) raise(SIGTERM);
	if (x->addr == T0_REG && value == 0xfe && fault("signal_enable")) raise(SIGTERM);
	if (x->addr == RESET_REG && value == 4) {
		release_written = true;
		if (fault("signal_release_write")) raise(SIGINT);
	}
	return 0;
}
static bool mtd_range(off_t offset, size_t len)
{
	if (offset < 0 || (uint64_t)offset > RAM_SIZE ||
	    len > RAM_SIZE - (uint64_t)offset) {
		violation("out of bounds MTD I/O");
		errno = EINVAL;
		return false;
	}
	if (reset_value != 6 || t0_value != 0xff || t1_value != 0xff)
		violation("MRAM accessed before hold and disable");
	return true;
}
static ssize_t mock_pwrite(int fd, const void *buf, size_t len, off_t offset)
{
	if (fd != MTD_FD) {
		violation("unexpected pwrite fd");
		errno = EBADF;
		return -1;
	}
	printf("MOCK write %llx %zu\n", (unsigned long long)offset, len);
	if (!mtd_range(offset, len) || fault("write_error")) return -1;
	if (offset && fault("write_after_data")) return -1;
	if (is_mode("short_then_error") && offset) {
		if (fault("short_then_error")) return -1;
	}
	if (fault("write_zero")) return 0;
	if (fault("write_eintr")) { errno = EINTR; return -1; }
	if ((is_mode("short_io") || is_mode("short_then_error")) && len > 7) len = 7;
	memcpy(ram + offset, buf, len);
	if (fault("signal_write")) raise(SIGTERM);
	return len;
}
static ssize_t mock_pread(int fd, void *buf, size_t len, off_t offset)
{
	if (fd != MTD_FD) {
		if (fault("file_read_error")) return -1;
		if (fault("file_read_zero")) return 0;
		if (fault("file_read_eintr")) { errno = EINTR; return -1; }
		if (is_mode("short_file") && len > 13) len = 13;
		return pread(fd, buf, len, offset);
	}
	printf("MOCK read %llx %zu\n", (unsigned long long)offset, len);
	if (!mtd_range(offset, len) || fault("read_error")) return -1;
	if (offset && fault("read_after_data")) return -1;
	if (fault("read_zero")) return 0;
	if (fault("read_eintr")) { errno = EINTR; return -1; }
	if (is_mode("short_io") && len > 5) len = 5;
	memcpy(buf, ram + offset, len);
	if (fault("corrupt_data") ||
	    (offset == 16 && fault("corrupt_bss")) ||
	    (offset == 0x2000 && fault("corrupt_second_data")) ||
	    (offset == 0x3011 && fault("corrupt_late_bss")))
		((uint8_t *)buf)[0] ^= 1;
	if (fault("signal_read")) raise(SIGINT);
	return len;
}

#define open mock_open
#define close mock_close
#define fstat mock_fstat
#define realpath mock_realpath
#define flock mock_flock
#define ioctl mock_ioctl
#define pread mock_pread
#define pwrite mock_pwrite
#define usleep mock_usleep
#define main erbctl_main
#include "../linux/tools/erbium-loader.c"
#include "../linux/tools/erbctl.c"
#undef main
#undef open
#undef close
#undef fstat
#undef realpath
#undef flock
#undef ioctl
#undef pread
#undef pwrite
#undef usleep

int main(int argc, char **argv)
{
	mode = getenv("ERBCTL_MOCK_MODE");
	if (!mode) mode = "";
	memset(ram, 0xa5, sizeof(ram));
	if (is_mode("reset_bad")) reset_value = 0;
	if (is_mode("already_held")) reset_value = 6;
	int repeats = getenv("ERBCTL_MOCK_REPEAT") ? 2 : 1;
	int rc = 0;
	for (int i = 0; i < repeats; i++) {
		printf("MOCK run %d\n", i + 1);
		/* Poison all MRAM on reload: omitted BSS writes cannot hide behind
		 * zero-initialized backing storage or a previous successful load. */
		if (i) memset(ram, 0xa5, sizeof(ram));
		rc = erbctl_main(argc, argv);
		printf("MOCK result %d\n", rc);
		if (rc) break;
	}
	const char *dump = getenv("ERBCTL_MOCK_DUMP");
	if (dump) {
		FILE *file = fopen(dump, "wb");
		if (!file || fwrite(ram, 1, 65536, file) != 65536 || fclose(file))
			return 99;
	}
	printf("MOCK final %llx %llx %llx %llx %d %d %d\n",
	       (unsigned long long)reset_value, (unsigned long long)t0_value,
	       (unsigned long long)t1_value, (unsigned long long)boot_value,
	       control_opens, mtd_opens, violations);
	return rc;
}
