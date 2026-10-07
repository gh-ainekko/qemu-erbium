// SPDX-License-Identifier: GPL-2.0
/*
 * erbctl - poke the Erbium xSPI control plane through /dev/erbiumN.
 *
 *   erbctl [-d /dev/erbium0] info
 *   erbctl reg <off> [<val>]            SCCR register read/write
 *   erbctl mem <xspi-addr> <len>        hexdump of the xSPI map
 *   erbctl memw <xspi-addr> <hex..>     write bytes (multiples of 8 recommended)
 *   erbctl mem32 <xspi-addr> [<val>]    32-bit sysreg style access (8-byte beat)
 *   erbctl sfdp [<off> <len>]           SFDP dump
 *   erbctl rates <cmd> <addr> <data>    52h Set Rate (0=S1 2=S4 3=D4 6=S8 7=D8)
 *   erbctl reset                        99h chip reset
 *   erbctl hold                       CPU warm-reset hold (preserves MRAM)
 *   erbctl load ELF [--verify] [--start | --check] [--mtd /dev/mtd0]
 *   erbctl console [TTY] [--baud N]     direct UART (default /dev/ttyAMA1)
 *   erbctl job <word> [timeout_ms]      write Mailbox0, wait for Mailbox1 != 0
 */
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/file.h>
#include <time.h>
#include <unistd.h>
#include "erbium-xspi.h"
#include "erbium-loader.h"
#include "erbium-console.h"

static int fd;

static void die(const char *what)
{
	fprintf(stderr, "erbctl: %s: %s\n", what, strerror(errno));
	exit(1);
}

static uint64_t num(const char *s)
{
	return strtoull(s, NULL, 0);
}

static void hexdump(uint64_t base, const uint8_t *b, size_t n)
{
	for (size_t i = 0; i < n; i += 16) {
		printf("%010llx ", (unsigned long long)(base + i));
		for (size_t j = i; j < i + 16 && j < n; j++)
			printf(" %02x", b[j]);
		printf("\n");
	}
}

static int mem_read(uint64_t addr, void *buf, uint32_t len)
{
	struct erbium_mem_xfer x = { .addr = addr, .buf = (uintptr_t)buf, .len = len };

	return ioctl(fd, ERBIUM_IOC_MEM_READ, &x);
}

static int mem_write(uint64_t addr, const void *buf, uint32_t len)
{
	struct erbium_mem_xfer x = { .addr = addr, .buf = (uintptr_t)buf, .len = len };

	return ioctl(fd, ERBIUM_IOC_MEM_WRITE, &x);
}

static uint32_t sysreg_read(uint64_t addr)
{
	uint64_t v;

	if (mem_read(addr, &v, 8))
		die("mem read");
	return (uint32_t)v;
}

static void sysreg_write(uint64_t addr, uint32_t val)
{
	uint64_t v = val;

	if (mem_write(addr, &v, 8))
		die("mem write");
}

static uint64_t now_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000ULL + ts.tv_nsec / 1000000;
}

int main(int argc, char **argv)
{
	const char *dev = "/dev/erbium0";
	int i = 1;

	if (argc > 2 && !strcmp(argv[1], "-d")) {
		dev = argv[2];
		i = 3;
	}
	if (i >= argc) {
		fprintf(stderr, "usage: erbctl [-d dev] info|reg|mem|memw|mem32|sfdp|rates|reset|job|hold|load|console ...\n"
			"  console [TTY (default /dev/ttyAMA1)] [--baud N (default 115200)]\n"
			"  Direct UART; Ctrl-] exits on terminal stdin (pipe input is binary).\n");
		return 2;
	}
	const char *cmd = argv[i++];
	int rem = argc - i;
	char **a = argv + i;
	if (!strcmp(cmd, "console"))
		return erbctl_console(rem, a);
	if (!strcmp(cmd, "load"))
		return erbctl_load(dev, rem, a);
	if (!strcmp(cmd, "hold") && rem == 0)
		return erbctl_hold(dev);
	fd = open(dev, O_RDWR);
	if (fd < 0)
		die(dev);
	if (flock(fd, LOCK_EX | LOCK_NB))
		die("device busy (another cooperating erbctl process?)");

	if (!strcmp(cmd, "info")) {
		struct erbium_info in;

		if (ioctl(fd, ERBIUM_IOC_GET_INFO, &in))
			die("GET_INFO");
		printf("id0 %08x id1 %08x cfg %08x status %08x rates %08x int_status %08x\n",
		       in.id0, in.id1, in.cfg, in.status, in.rates, in.int_status);
		printf("mram %llu KiB, read burst %u, write burst %u, latency %u cycles, driver rates %u/%u/%u\n",
		       (unsigned long long)in.mram_size / 1024, in.read_burst, in.write_burst,
		       in.latency_cycles, in.cur_rates & 0xff, (in.cur_rates >> 8) & 0xff,
		       (in.cur_rates >> 16) & 0xff);
	} else if (!strcmp(cmd, "reg") && rem >= 1) {
		struct erbium_reg r = { .offset = num(a[0]) };

		if (rem >= 2) {
			r.value = num(a[1]);
			if (ioctl(fd, ERBIUM_IOC_REG_WRITE, &r))
				die("REG_WRITE");
		}
		if (ioctl(fd, ERBIUM_IOC_REG_READ, &r))
			die("REG_READ");
		printf("%08x\n", r.value);
	} else if (!strcmp(cmd, "mem") && rem >= 2) {
		uint64_t addr = num(a[0]);
		uint32_t len = num(a[1]);
		uint8_t *buf = malloc(len);

		if (mem_read(addr, buf, len))
			die("MEM_READ");
		hexdump(addr, buf, len);
	} else if (!strcmp(cmd, "memw") && rem >= 2) {
		uint64_t addr = num(a[0]);
		uint8_t buf[4096];
		uint32_t n = 0;

		for (int k = 1; k < rem && n < sizeof(buf); k++)
			buf[n++] = num(a[k]);
		if (mem_write(addr, buf, n))
			die("MEM_WRITE");
	} else if (!strcmp(cmd, "mem32") && rem >= 1) {
		uint64_t addr = num(a[0]);

		if (rem >= 2)
			sysreg_write(addr, num(a[1]));
		printf("%08x\n", sysreg_read(addr));
	} else if (!strcmp(cmd, "sfdp")) {
		struct erbium_sfdp_xfer x = { .offset = rem >= 1 ? num(a[0]) : 0,
					      .len = rem >= 2 ? num(a[1]) : 0x40 };
		uint8_t *buf = malloc(x.len);

		x.buf = (uintptr_t)buf;
		if (ioctl(fd, ERBIUM_IOC_SFDP_READ, &x))
			die("SFDP_READ");
		hexdump(x.offset, buf, x.len);
	} else if (!strcmp(cmd, "rates") && rem >= 3) {
		struct erbium_rates r = { .cmd = num(a[0]), .addr = num(a[1]), .data = num(a[2]) };

		if (ioctl(fd, ERBIUM_IOC_SET_RATES, &r))
			die("SET_RATES");
	} else if (!strcmp(cmd, "reset")) {
		if (ioctl(fd, ERBIUM_IOC_RESET))
			die("RESET");
	} else if (!strcmp(cmd, "job") && rem >= 1) {
		uint64_t mb0 = ERBIUM_XSPI_SYSREGS_BASE + ERBIUM_SYSREG_MAILBOX0;
		uint64_t mb1 = ERBIUM_XSPI_SYSREGS_BASE + ERBIUM_SYSREG_MAILBOX1;
		uint64_t deadline = now_ms() + (rem >= 2 ? num(a[1]) : 5000);
		uint32_t v;

		sysreg_write(mb1, 0);
		sysreg_write(mb0, num(a[0]));
		while ((v = sysreg_read(mb1)) == 0) {
			if (now_ms() > deadline) {
				fprintf(stderr, "erbctl: job timed out\n");
				return 1;
			}
			usleep(1000);
		}
		printf("%08x\n", v);
	} else {
		fprintf(stderr, "erbctl: bad command\n");
		return 2;
	}
	return 0;
}
