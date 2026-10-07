/* SPDX-License-Identifier: Apache-2.0 */
#include <stdint.h>
#include "mem.h"
#include "interp.h"
#include "platform.h"

#define SYS_CONFIG 0x02000008u
#define UART_BASE  0x02004000u
#define ESR_MTIME  0x80f40200u
#define ESR_TIME_CONFIG 0x80f40210u
#define UART_STATUS (UART_BASE + 0x18)
#define TX_FULL (1u << 1)
#define TX_EMPTY (1u << 0)
#define RX_READY (1u << 2)

/*
 * Current Erbium sysemu: 200MHz cycle -> /5 prescaler clock -> /20 MTIME.
 * Not TamaGo's historical 50us/tick profile. Override for measured hardware.
 */
#ifndef ERBIUM_TIMER_HZ
#define ERBIUM_TIMER_HZ 2000000u
#endif
#if ERBIUM_TIMER_HZ < 1000000u || ERBIUM_TIMER_HZ % 1000000u
#error "ERBIUM_TIMER_HZ must be a whole number of MHz"
#endif

static inline uint32_t read32(uintptr_t addr) {
    return *(volatile uint32_t *)addr;
}
static inline void write32(uintptr_t addr, uint32_t value) {
    *(volatile uint32_t *)addr = value;
}
#ifdef SMALLVM_SELFTEST
static int protocol_enabled;
#endif
void smallvm_platform_start_protocol(void) {
#ifdef SMALLVM_SELFTEST
    protocol_enabled = 1;
#endif
}
void smallvm_platform_flush(void) {
    while (!(read32(UART_STATUS) & TX_EMPTY)) __asm__ volatile("nop");
}

void hardwareInit(void) {
    write32(SYS_CONFIG, read32(SYS_CONFIG) | (1u << 6));
    /* Erbium UART input clock is 200MHz/30; divisor rounded for 115200. */
    write32(UART_BASE, 58);
    write32(UART_BASE + 0x30, 0); /* no UART IRQ; cooperative polling */
    *(volatile uint64_t *)(uintptr_t)ESR_TIME_CONFIG = 20;
    boardInit();
}

int recvBytes(uint8 *buf, int count) {
    int n = 0;
    if (!buf || count <= 0) return 0;
#ifdef SMALLVM_SELFTEST
    if (!protocol_enabled) return 0;
#endif
    while (n < count && (read32(UART_STATUS) & RX_READY))
        buf[n++] = (uint8)read32(UART_BASE + 0x10);
    return n;
}

int sendBytes(uint8 *buf, int start, int end) {
    if (!buf || start < 0 || end <= start) return 0;
#ifdef SMALLVM_SELFTEST
    /* Tests exercise framing without interleaving binary output with raw reports. */
    if (!protocol_enabled) return end - start;
#endif
    int n = 0;
    while (start + n < end && !(read32(UART_STATUS) & TX_FULL))
        write32(UART_BASE + 0x08, buf[start + n++]);
    return n;
}

uint64 totalMicrosecs(void) {
    uint64_t ticks = *(volatile uint64_t *)(uintptr_t)ESR_MTIME;
    return ticks / (ERBIUM_TIMER_HZ / 1000000u);
}
uint32 microsecs(void) { return (uint32)totalMicrosecs(); }
uint32 millisecs(void) { return (uint32)(totalMicrosecs() / 1000u); }
uint32 seconds(void) { return (uint32)(totalMicrosecs() / 1000000u); }
void handleMicosecondClockWrap(void) { /* native 64-bit timer needs no extension */ }
void delay(unsigned long msecs) {
    uint64_t start = totalMicrosecs();
    uint64_t duration = (uint64_t)msecs * 1000u;
    while (totalMicrosecs() - start < duration) __asm__ volatile("nop");
}
void lightSleep(int msecs) { if (msecs > 0) delay((unsigned long)msecs); }
void deepSleep(int secs) { if (secs > 0) delay((unsigned long)secs * 1000u); }
void restartSerial(void) { /* transport remains enabled */ }
const char *boardType(void) { return "Erbium"; }

void smallvm_platform_diag(const char *text) {
#ifdef SMALLVM_SELFTEST
    if (!text) return;
    while (*text) {
        while (read32(UART_STATUS) & TX_FULL) __asm__ volatile("nop");
        write32(UART_BASE + 0x08, (uint8_t)*text++);
    }
#else
    (void)text;
#endif
}

#ifdef SMALLVM_SELFTEST
/* Shared host/firmware test suite reporting seam. */
void test_report(const char *line) {
    smallvm_platform_diag(line);
    smallvm_platform_diag("\n");
}
#endif

/* Fatal state is captured in MRAM for debugger/host inspection. No raw bytes
 * contaminate a normal protocol session, even on a trap. */
volatile uintptr_t smallvm_fault_state[4];
void smallvm_fault(uintptr_t cause, uintptr_t pc, uintptr_t value, uintptr_t sp) {
    smallvm_fault_state[0] = cause;
    smallvm_fault_state[1] = pc;
    smallvm_fault_state[2] = value;
    smallvm_fault_state[3] = sp;
#ifdef SMALLVM_SELFTEST
    smallvm_platform_diag("SmallVM FAULT cause/pc/value/sp:");
    const uintptr_t words[] = {cause, pc, value, sp};
    for (unsigned i = 0; i < 4; ++i) {
        char hex[18];
        hex[0] = ' ';
        for (unsigned j = 0; j < 16; ++j)
            hex[j + 1] = "0123456789abcdef"[(words[i] >> ((15 - j) * 4)) & 15];
        hex[17] = 0;
        smallvm_platform_diag(hex);
    }
    smallvm_platform_diag("\n");
    smallvm_platform_flush();
#endif
    for (;;) __asm__ volatile("wfi");
}
