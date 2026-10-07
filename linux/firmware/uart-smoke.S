/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Hermetic direct-UART serial peer, not a mailbox console.
 * CPU Shakti UART at 0x02004000; SystemConfig bit 6 selects GPIO9/10.
 * Polling deliberately leaves RX queued whenever TX's finite FIFO is full.
 * Stream backpressure is not real-wire RTS/CTS or physical overrun modeling.
 */
    .option norelax
    .section .text,"ax",@progbits
    .globl _start
_start:
    la sp, stack_top
    li t0, 0x02000008
    lw t1, 0(t0)
    ori t1, t1, 0x40
    sw t1, 0(t0)                 /* Preserve unrelated pinmux/config bits. */
    fence iorw, iorw

    li s0, 0x02004000
    li t1, 58                   /* Provisional emulator divider, not a board BSP. */
    sw t1, 0x00(s0)
    li t1, 0x100                /* 8 data bits, no parity, one stop bit. */
    sw t1, 0x28(s0)
    fence iorw, iorw
    /* No IRQ-register access: HAL/emulator IRQ offsets differ from built RTL.
     * No CSRs/runtime/interrupts are needed for this polling fixture. */
    la s1, banner
banner_next:
    lbu t2, 0(s1)
    beqz t2, echo
banner_wait:
    lw t1, 0x18(s0)
    andi t1, t1, 2              /* TX_FULL */
    bnez t1, banner_wait
    sw t2, 0x08(s0)
    fence iorw, iorw
    addi s1, s1, 1
    j banner_next
echo:
    lw t1, 0x18(s0)
    andi t2, t1, 2
    bnez t2, echo               /* Do not dequeue RX while TX is full. */
    andi t2, t1, 4              /* RX_NOT_EMPTY */
    beqz t2, echo
    lw t2, 0x10(s0)
    andi t2, t2, 0xff
    sw t2, 0x08(s0)             /* Byte-transparent, including NUL/XON/XOFF. */
    fence iorw, iorw
    j echo

    .section .rodata,"a",@progbits
banner:
    .asciz "ERBIUM-UART-SMOKE-v1\r\n"

    .section .bss,"aw",@nobits
    .balign 16
    .space 256
    .globl stack_top
stack_top:
