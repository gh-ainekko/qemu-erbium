/* SPDX-License-Identifier: Apache-2.0 */
OUTPUT_ARCH(riscv)
ENTRY(_start)
MEMORY {
    /* Leave the first 64KiB for future host control/rings. No durable region. */
    MRAM (rwx) : ORIGIN = 0x40010000, LENGTH = 16M - 64K
}
PHDRS {
    text PT_LOAD FLAGS(5);
    data PT_LOAD FLAGS(6);
    tls PT_TLS FLAGS(4);
}
SECTIONS {
    .text : {
        KEEP(*(.text.startup))
        *(.text .text.*)
    } > MRAM :text
    .rodata : {
        *(.rodata .rodata.* .srodata .srodata.*)
    } > MRAM :text
    . = ALIGN(16);
    .data : {
        *(.data .data.*)
        __global_pointer$ = . + 0x800;
        *(.sdata .sdata.*)
    } > MRAM :data
    .tdata ALIGN(16) : {
        __tls_base = .;
        *(.tdata .tdata.*)
    } > MRAM :data :tls
    __bss_start = .;
    .tbss (NOLOAD) : {
        *(.tbss .tbss.*)
        . = ALIGN(8);
    } > MRAM :data :tls
    .bss MAX(ALIGN(16), ADDR(.tbss) + SIZEOF(.tbss)) (NOLOAD) : {
        *(.sbss .sbss.* .bss .bss.*)
        *(COMMON)
        . = ALIGN(16);
        __bss_end = .;
    } > MRAM :data
    .stack (NOLOAD) : {
        __stack_bottom = .;
        . += 32K;
        __stack_top = .;
        . += 4K;
        __fault_stack_top = .;
    } > MRAM :data
    __image_end = .;
    ASSERT(__image_end <= ORIGIN(MRAM) + LENGTH(MRAM), "SmallVM MRAM overflow")
    ASSERT(__image_end < 0x80000000, "VM handles require low aligned addresses")
    ASSERT((__bss_start & 7) == 0, "BSS must be eight-byte aligned")
    ASSERT(__tls_base == ADDR(.tdata), "RISC-V TP must equal TLS segment base")
    /DISCARD/ : { *(.comment .note .note.*) }
}
