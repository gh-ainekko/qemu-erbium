/* SPDX-License-Identifier: Apache-2.0 */
#include "mem.h"
#include "interp.h"
#include "persist.h"
#include "platform.h"

#ifdef SMALLVM_SELFTEST
/* Kept volatile so successful startup cannot be constant-folded by the compiler.
 * The runner can prefill MRAM with 0xa5 before loading this ELF. */
static volatile unsigned initialized_probe = 0x12345678;
static volatile unsigned zero_probe[16];
static _Thread_local volatile unsigned tls_initialized_probe = 0x87654321;
static _Thread_local volatile unsigned tls_zero_probe;

static int startup_selftest(void) {
    if (initialized_probe != 0x12345678 ||
        tls_initialized_probe != 0x87654321 || tls_zero_probe != 0)
        return 1;
    for (unsigned i = 0; i < 16; i++) if (zero_probe[i]) return 1;
    smallvm_platform_diag("PASS startup-data-bss-tls\n");
    return 0;
}
#endif

int main(void) {
    /* UART must work before allocation/layout checks can panic. */
    hardwareInit();
#ifdef SMALLVM_SELFTEST
    extern int smallvm_selftest(void);
    smallvm_platform_diag("SmallVM selftest BEGIN\n");
    if (startup_selftest()) {
        smallvm_platform_diag("SmallVM selftest FAIL startup-data-bss-tls\n");
        smallvm_platform_flush();
        for (;;) __asm__ volatile("wfi");
    }
    if (smallvm_platform_timer_selftest()) {
        smallvm_platform_diag("SMALLVM_SELFTEST FAIL timer calibration\n");
        smallvm_platform_flush();
        for (;;) __asm__ volatile("wfi");
    }
    uint64 start = totalMicrosecs();
    /* A broken timer should fail visibly, not hang inside delay(). */
    for (unsigned spin = 0; spin < 1000000 && totalMicrosecs() == start; ++spin)
        __asm__ volatile("nop");
    if (totalMicrosecs() == start) {
        smallvm_platform_diag("SmallVM selftest FAIL timer stopped\n");
        smallvm_platform_flush();
        for (;;) __asm__ volatile("wfi");
    }
    delay(1);
    if (totalMicrosecs() <= start) {
        smallvm_platform_diag("SmallVM selftest FAIL timer\n");
        smallvm_platform_flush();
        for (;;) __asm__ volatile("wfi");
    }
    int failed = smallvm_selftest();
    smallvm_platform_diag(failed ? "SmallVM selftest FAIL\n" : "SmallVM selftest PASS\n");
    smallvm_platform_flush();
    if (failed) for (;;) __asm__ volatile("wfi");
    /* Do not let test roots/tasks/code leak into the live protocol VM. */
    initTasks();
    memClear();
    clearPersistentMemory();
    restoreScripts();
    /* Drain test-generated frames into the selftest sink before enabling UART. */
    processMessage();
#else
    memInit();
    primsInit();
    restoreScripts(); /* upstream unrecognized-platform branch: volatile RAM */
#endif
    smallvm_platform_start_protocol();
    /* A real binary-protocol readiness frame, never an unframed debug banner.
     * outputString() intentionally skips output before an IDE has connected. */
    char started[] = "\002Started MicroBlocks on Erbium";
    waitAndSendMessage(outputValueMsg, 255, sizeof(started) - 1, started);
    if (boardStartAtBoot()) startAll();
    vmLoop();
    return 0;
}
