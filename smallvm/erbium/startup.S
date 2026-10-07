/* SPDX-License-Identifier: Apache-2.0 */
/* Integer-only minion reset. Only hart 0 owns VM state. */
    .section .text.startup, "ax", @progbits
    .balign 4
    .global _start
_start:
    csrwi mie, 0
    csrwi sie, 0
    csrci mstatus, 8
    csrr t0, mhartid
    bnez t0, .Lpark
    csrwi satp, 0
    csrwi 0x7c0, 0              /* Erbium matp */
    csrwi 0x7d2, 0              /* menable_shadows */
    csrwi 0x7e0, 0              /* mcache_control */
    csrwi 0x805, 0              /* tensor_mask */
    csrwi mip, 0
    li t0, 0x200
    csrw 0x810, t0             /* ucache_control reset value */
    csrwi mscratch, 0
    .option push
    .option norelax
    la gp, __global_pointer$
    la tp, __tls_base
    .option pop
    la sp, __stack_top
    la t0, smallvm_trap_entry
    csrw mtvec, t0
    /* ELF PT_LOAD initializes data (VMA=LMA), including TLS data. */
    la t0, __bss_start
    la t1, __bss_end
.Lzero:
    bgeu t0, t1, .Lmain
    sd zero, 0(t0)
    addi t0, t0, 8
    j .Lzero
.Lmain:
    li s0, 0
    call main
.Lpark:
    wfi
    j .Lpark

    /* Fatal traps do not return. Use a separate stack even for stack faults. */
    .balign 4
    .global smallvm_trap_entry
smallvm_trap_entry:
    csrr a0, mcause
    csrr a1, mepc
    csrr a2, mtval
    mv a3, sp
    la sp, __fault_stack_top
    .option push
    .option norelax
    la gp, __global_pointer$
    la tp, __tls_base
    .option pop
    call smallvm_fault
    j .Lpark
