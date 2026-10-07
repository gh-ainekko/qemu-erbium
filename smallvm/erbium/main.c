/* SPDX-License-Identifier: Apache-2.0 */
#include "mem.h"
#include "interp.h"
#include "persist.h"
#include "platform.h"

int main(void) {
    /* UART must work before allocation/layout checks can panic. */
    hardwareInit();
    memInit();
    primsInit();
    restoreScripts(); /* upstream unrecognized-platform branch: volatile RAM */
#ifdef SMALLVM_SELFTEST
    extern int smallvm_selftest(void);
    smallvm_platform_diag("SmallVM selftest BEGIN\n");
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
#endif
    smallvm_platform_start_protocol();
    if (boardStartAtBoot()) startAll();
    vmLoop();
    return 0;
}
