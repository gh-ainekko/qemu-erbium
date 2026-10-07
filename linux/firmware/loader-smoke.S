/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Stackless, MRAM-only rv64imc loader smoke test. No runtime, CSRs or UART.
 * Mailboxes are 32-bit registers on an eight-byte stride in the CPU map.
 * Host/guest-test constants and the corresponding xSPI addresses: README.md.
 */
    .option norelax
    .section .text,"ax",@progbits
    .globl _start
    .globl smoke_text_tail
smoke_segment_start:
    /* Loading the segment base as the PC instead of e_entry must fail. */
    j bad_entry
    .org 0x40, 0
_start:
    la t0, smoke_data
    lwu t1, 0(t0)
    li t2, 0x13579bdf
    bne t1, t2, bad_data
    la t0, smoke_data_tail
    lwu t1, 0(t0)
    li t2, 0x2468ace0
    bne t1, t2, bad_data
    lbu t1, 6(t0)
    li t2, 0x33
    bne t1, t2, bad_data

    /* Both file-backed segments extend past a 4 KiB boundary. */
    la t0, smoke_text_tail
    lwu t1, 0(t0)
    li t2, 0x5eedc0de
    bne t1, t2, bad_text
    lbu t1, 6(t0)
    li t2, 0xc3
    bne t1, t2, bad_text

    la t0, smoke_bss
    la t2, smoke_bss_end
1:
    lbu t1, 0(t0)
    bnez t1, bad_bss
    addi t0, t0, 1
    bne t0, t2, 1b

    /* A second run without reloading/zeroing must fail the check above. */
    la t0, smoke_bss
    li t1, 0xa5
2:
    sb t1, 0(t0)
    addi t0, t0, 1
    bne t0, t2, 2b
    fence rw, rw
    li t0, 0x02000068
    li t1, 0x4c44534d
    sw t1, 0(t0)                 /* MAILBOX0: LDSM signature */
    li t1, 0x600d600d
    sw t1, 8(t0)                 /* MAILBOX1: success, published last */
3:
    j 3b                        /* Stay alive; never invoke an exit trap. */

bad_data:
    li t1, 0xbad00001
    j fail
bad_bss:
    li t1, 0xbad00002
    j fail
bad_text:
    li t1, 0xbad00003
    j fail
bad_entry:
    li t1, 0xbad00004
fail:
    li t0, 0x02000068
    li t2, 0x4641494c
    sw t2, 0(t0)                 /* MAILBOX0: FAIL signature */
    sw t1, 8(t0)
4:
    j 4b

    .org 0x1400, 0x5a
    /* Keep the odd-length tail in a non-instruction input section so the
     * assembler does not add a final two-byte instruction-alignment pad. */
    .section .text_tail,"a",@progbits
smoke_text_tail:
    .word 0x5eedc0de
    .byte 0xa1, 0xb2, 0xc3       /* Deliberately non-eight-byte filesz. */

    .section .data,"aw",@progbits
    .globl smoke_data
    .globl smoke_data_tail
smoke_data:
    .word 0x13579bdf
    .space 0x1000 - 4, 0xa5
smoke_data_tail:
    .word 0x2468ace0
    .byte 0x11, 0x22, 0x33

    .section .bss,"aw",@nobits
    .balign 8
    .globl smoke_bss
    .globl smoke_bss_end
smoke_bss:
    .space 0x1020
smoke_bss_end:
