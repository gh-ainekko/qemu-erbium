/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
/*
 * User API of the Erbium xSPI target driver (/dev/erbiumN).
 *
 * The MRAM is exposed as an MTD device; this character device gives access
 * to everything else reachable through the xSPI port: the xSPI slave
 * control/configuration registers (SCCR), the SFDP ROM, and arbitrary
 * locations of the xSPI address map (system registers, SRAM, ...).
 */
#ifndef _UAPI_LINUX_ERBIUM_XSPI_H
#define _UAPI_LINUX_ERBIUM_XSPI_H

#include <stdint.h>
typedef uint8_t __u8; typedef uint32_t __u32; typedef uint64_t __u64;
#include <sys/ioctl.h>

/* xSPI address map (Erbium xSPI slave, see TRM "xSPI address translation") */
#define ERBIUM_XSPI_MRAM_BASE		0x00000000ULL
#define ERBIUM_XSPI_SYSREGS_BASE	0x40000000ULL
#define ERBIUM_XSPI_SRAM_BASE		0x4000C000ULL
#define ERBIUM_XSPI_SCCR_BASE		0x4000F000ULL

/* system register offsets from ERBIUM_XSPI_SYSREGS_BASE */
#define ERBIUM_SYSREG_SYS_INTERRUPT	0x20
#define ERBIUM_SYSREG_MAILBOX0		0x68
#define ERBIUM_SYSREG_MAILBOX1		0x70

/* SCCR register offsets */
#define ERBIUM_SCCR_ID0			0x00
#define ERBIUM_SCCR_ID1			0x08
#define ERBIUM_SCCR_CFG			0x10
#define ERBIUM_SCCR_STATUS		0x18
#define ERBIUM_SCCR_ADDR		0x20
#define ERBIUM_SCCR_RATES		0x28
#define ERBIUM_SCCR_INT_STATUS		0x30

/* xSPI rates (SCCR.RATES fields and 52h Set Rate bytes) */
#define ERBIUM_RATE_S1			0
#define ERBIUM_RATE_S4			2
#define ERBIUM_RATE_D4			3
#define ERBIUM_RATE_S8			6
#define ERBIUM_RATE_D8			7

struct erbium_reg {
	__u32 offset;		/* SCCR offset (0x00..0x3f, 8-byte stride) */
	__u32 value;
};

struct erbium_mem_xfer {
	__u64 addr;		/* xSPI address */
	__u64 buf;		/* user pointer */
	__u32 len;		/* bytes; multiple of 8 recommended for writes */
	__u32 flags;		/* reserved, 0 */
};

struct erbium_sfdp_xfer {
	__u32 offset;		/* SFDP byte address, 4-byte aligned */
	__u32 len;		/* bytes, multiple of 4 */
	__u64 buf;		/* user pointer */
};

struct erbium_rates {
	__u8 cmd;
	__u8 addr;
	__u8 data;
	__u8 pad;
};

struct erbium_info {
	__u32 id0;
	__u32 id1;
	__u32 cfg;
	__u32 status;
	__u32 rates;		/* SCCR.RATES raw */
	__u32 int_status;	/* last read of interrupt_status (rclr!) */
	__u64 mram_size;
	__u32 read_burst;	/* max bytes per Read Memory frame */
	__u32 write_burst;	/* max bytes per Write Memory frame */
	__u32 latency_cycles;
	__u32 cur_rates;	/* rates the driver is currently using (cmd|addr<<8|data<<16) */
};

#define ERBIUM_IOC_MAGIC		'E'
#define ERBIUM_IOC_GET_INFO		_IOR(ERBIUM_IOC_MAGIC, 0x00, struct erbium_info)
#define ERBIUM_IOC_REG_READ		_IOWR(ERBIUM_IOC_MAGIC, 0x01, struct erbium_reg)
#define ERBIUM_IOC_REG_WRITE		_IOW(ERBIUM_IOC_MAGIC, 0x02, struct erbium_reg)
#define ERBIUM_IOC_MEM_READ		_IOW(ERBIUM_IOC_MAGIC, 0x03, struct erbium_mem_xfer)
#define ERBIUM_IOC_MEM_WRITE		_IOW(ERBIUM_IOC_MAGIC, 0x04, struct erbium_mem_xfer)
#define ERBIUM_IOC_SFDP_READ		_IOW(ERBIUM_IOC_MAGIC, 0x05, struct erbium_sfdp_xfer)
#define ERBIUM_IOC_SET_RATES		_IOW(ERBIUM_IOC_MAGIC, 0x06, struct erbium_rates)
#define ERBIUM_IOC_RESET		_IO(ERBIUM_IOC_MAGIC, 0x07)

#endif /* _UAPI_LINUX_ERBIUM_XSPI_H */
