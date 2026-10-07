/* SPDX-License-Identifier: Apache-2.0 */
/* Transport test only: real MMIO UART, no SmallVM/IDE protocol implementation. */
#include <stdint.h>
static uint32_t rd(uintptr_t addr) { return *(volatile uint32_t *)addr; }
static void wr(uintptr_t addr, uint32_t value) { *(volatile uint32_t *)addr = value; }
static void tx(uint8_t byte) {
    while (rd(0x02004018) & 2) __asm__ volatile("nop");
    wr(0x02004008, byte);
}
_Noreturn void smallvm_fault(uint64_t cause, uint64_t pc, uint64_t value, uint64_t sp) {
    (void)cause; (void)pc; (void)value; (void)sp;
    for (;;) __asm__ volatile("wfi");
}
int main(void) {
    wr(0x02000008, rd(0x02000008) | (1u << 6));
    tx('R'); tx('E'); tx('A'); tx('D'); tx('Y');
    for (;;) {
        if (rd(0x02004018) & 4) tx((uint8_t)rd(0x02004010));
    }
}
