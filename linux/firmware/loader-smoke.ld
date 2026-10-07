/* SPDX-License-Identifier: GPL-2.0 */
OUTPUT_ARCH(riscv)
ENTRY(_start)
PHDRS
{
    text PT_LOAD FLAGS(5);       /* R | X */
    data PT_LOAD FLAGS(6);       /* R | W, including the zero-filled tail */
}
SECTIONS
{
    .text 0x40010000 : { KEEP(*(.text)) KEEP(*(.text_tail)) } :text
    .data 0x40013000 : { KEEP(*(.data)) } :data
    /* Leave an additional zero-filled gap after the file-backed data. */
    .bss 0x40015000 (NOLOAD) : { KEEP(*(.bss)) } :data
    .riscv.attributes 0 : { *(.riscv.attributes) } :NONE
    /DISCARD/ : { *(.comment) *(.note*) }

    ASSERT(_start == 0x40010040, "guest test expects entry 0x40010040")
    ASSERT(SIZEOF(.text) == 0x1407, "unexpected text fixture size")
    ASSERT(SIZEOF(.data) == 0x1007, "unexpected data fixture size")
    ASSERT(smoke_bss == 0x40015000, "unexpected BSS start")
    ASSERT(smoke_bss_end == 0x40016020, "unexpected BSS end")
}
