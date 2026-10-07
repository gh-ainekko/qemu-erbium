/* SPDX-License-Identifier: GPL-2.0 */
OUTPUT_ARCH(riscv)
ENTRY(_start)
PHDRS
{
    text PT_LOAD FLAGS(5);
    data PT_LOAD FLAGS(6);
}
SECTIONS
{
    .text 0x40010000 : { KEEP(*(.text)) *(.rodata*) } :text
    .bss 0x40011000 (NOLOAD) : { *(.bss*) } :data
    .riscv.attributes 0 : { *(.riscv.attributes) } :NONE
    /DISCARD/ : { *(.comment) *(.note*) }
    ASSERT(_start == ADDR(.text), "ELF entry must be the real start")
    ASSERT(SIZEOF(.text) < 0x1000, "UART fixture text overlaps stack")
    ASSERT(stack_top <= 0x41000000, "UART fixture must remain MRAM-only")
}
