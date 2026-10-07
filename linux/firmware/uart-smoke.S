/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Hermetic direct-UART serial peer, not a mailbox console.
 * CPU Shakti UART at 0x02004000; SystemConfig bit 6 selects GPIO9/10.
 * RX echo runs ONLY in the machine-external ISR (PLIC source 3); main sleeps
 * in WFI. The ISR leaves RX queued whenever TX's finite FIFO is full.
 * Stream backpressure is not real-wire RTS/CTS or physical overrun modeling.
 * Mailbox0 counts UART claims, Mailbox1 counts WFI entries. On a fatal trap,
 * Mailbox0 instead holds mtval and Mailbox1 is 0xbad000xx (xx=mcause low byte).
 * These are diagnostic assertions ONLY, never console data.
 * Checked against sw-sysemu/devices/plic_{dev,er}.h and corrected Shakti RTL
 * registers: PLIC CPU base 0xa0000000, context 0 M, 32-bit accesses;
 * UART mask 0x50, sticky RAW 0x48 acknowledged by XOR of ONLY observed bits.
 */
    .option norelax
    .section .text,"ax",@progbits
    .globl _start
_start:
    csrci mstatus, 8             /* No interrupt before stack/vector setup. */
    csrw mie, zero
    csrw mideleg, zero
    la sp, stack_top
    la t0, uart_trap
    csrw mtvec, t0              /* Direct mode; four-byte aligned below. */
    li t0, 0x02000068
    sw zero, 0(t0)
    sw zero, 8(t0)
    li t0, 0x02000008
    lw t1, 0(t0)
    ori t1, t1, 0x40
    sw t1, 0(t0)                 /* Preserve unrelated pinmux/config bits. */
    fence iorw, iorw

    li s0, 0x02004000
    sw zero, 0x50(s0)           /* Mask all UART sources during setup/banner. */
    li t1, 58                   /* Provisional emulator divider, not a board BSP. */
    sw t1, 0x00(s0)
    li t1, 0x100                /* 8 data bits, no parity, one stop bit. */
    sw t1, 0x28(s0)
    li t1, 15
    sw t1, 0x38(s0)             /* RX threshold, NOT an interrupt mask. */
    li t1, -257                 /* Clear sticky STATUS threshold bit 8 only. */
    sw t1, 0x18(s0)
    lw t1, 0x48(s0)
    andi t1, t1, 0x3ff
    sw t1, 0x48(s0)             /* RAW XOR: never write a blind all-ones ACK. */

    li t0, 0xa0000000
    li t1, 1
    sw t1, 0x0c(t0)             /* Priority of UART source 3. */
    li t0, 0xa0002000
    li t1, 8
    sw t1, 0(t0)                /* Enable source 3 in context 0 (machine). */
    sw zero, 0x80(t0)           /* Context 1 (supervisor) is unused. */
    li t0, 0xa0200000
    sw zero, 0(t0)              /* Context 0 threshold: accept priority > 0. */
    li t1, 3
    sw t1, 4(t0)               /* Complete a prior context-0 claim on reload. */
    fence iorw, iorw
    la s1, banner
banner_next:
    lbu t2, 0(s1)
    beqz t2, irq_enable
banner_wait:
    lw t1, 0x18(s0)
    andi t1, t1, 2              /* TX_FULL */
    bnez t1, banner_wait
    sw t2, 0x08(s0)
    fence iorw, iorw
    addi s1, s1, 1
    j banner_next
irq_enable:
    li t1, 4
    sw t1, 0x50(s0)             /* Only RX_NOT_EMPTY interrupt bit 2. */
    fence iorw, iorw
    li t1, 0x800
    csrw mie, t1               /* MEIE, no timer/software interrupt fallback. */
idle:
    csrci mstatus, 8
    lw t1, 0x58(s0)             /* Interrupts-disabled pending/empty check. */
    bnez t1, dispatch
    li t0, 0x02000070
    lw t1, 0(t0)
    addi t1, t1, 1
    sw t1, 0(t0)               /* Diagnostic WFI count, not console bytes. */
    /* WFI wakes on locally enabled MEIP even with global MIE=0. An interrupt
     * between the empty check and WFI either prevents sleep or wakes it.
     * Main never processes RX; all byte echo is proof of ISR execution. */
    wfi
dispatch:
    csrsi mstatus, 8
    j idle

    .balign 4
    .globl uart_trap
uart_trap:
    /* MIE is cleared by trap entry; no nested traps/interrupts. Save every
     * register modified here, keep a 16-byte-aligned stack, leave mepc intact.
     * No calls or modifications of ra, argument/saved regs, gp or tp occur. */
    addi sp, sp, -64
    sd t0, 0(sp)
    sd t1, 8(sp)
    sd t2, 16(sp)
    sd t3, 24(sp)
    sd t4, 32(sp)
    sd t5, 40(sp)
    sd t6, 48(sp)
    csrr t0, mcause
    li t1, 0x800000000000000b
    bne t0, t1, unexpected_trap
    li t4, 0x02004000
    li t5, 0xa0200004           /* Context-0 claim/complete; word accesses. */
    lw t3, 0(t5)
    beqz t3, trap_return        /* Spurious/deasserted external notification. */
    li t1, 3
    bne t3, t1, unexpected_trap
    li t0, 0x02000068
    lw t1, 0(t0)
    addi t1, t1, 1
    sw t1, 0(t0)               /* Diagnostic UART IRQ claim count. */
irq_echo:
    lw t1, 0x18(t4)
    andi t2, t1, 4
    beqz t2, irq_ack            /* RX empty: acknowledge sticky state. */
    andi t2, t1, 2
    bnez t2, irq_echo           /* TX full: do NOT dequeue and lose RX. */
    lw t2, 0x10(t4)
    andi t2, t2, 0xff
    sw t2, 0x08(t4)             /* Echo exists only in this IRQ handler. */
    fence iorw, iorw
    j irq_echo
irq_ack:
    li t1, -257
    sw t1, 0x18(t4)             /* Clear STATUS threshold separately from mask. */
    lw t1, 0x48(t4)
    andi t1, t1, 0x3ff
    sw t1, 0x48(t4)             /* XOR only currently set RAW bits. */
    fence iorw, iorw
    sw t3, 0(t5)               /* Complete after servicing/acknowledging UART. */
    fence iorw, iorw
trap_return:
    ld t0, 0(sp)
    ld t1, 8(sp)
    ld t2, 16(sp)
    ld t3, 24(sp)
    ld t4, 32(sp)
    ld t5, 40(sp)
    ld t6, 48(sp)
    addi sp, sp, 64
    mret
unexpected_trap:
    li t1, 0xbad00000
    andi t0, t0, 0xff
    or t1, t1, t0
    li t0, 0x02000070
    sw t1, 0(t0)               /* No serial corruption; host timeout fails. */
    csrr t2, mtval
    sw t2, -8(t0)              /* Fatal diagnostics replace count with mtval. */
    csrw mie, zero
1:
    wfi
    j 1b

    .section .rodata,"a",@progbits
banner:
    .asciz "ERBIUM-UART-SMOKE-v1\r\n"

    .section .bss,"aw",@nobits
    .balign 16
    .space 256
    .globl stack_top
stack_top:
