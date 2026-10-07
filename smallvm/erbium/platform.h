/* SPDX-License-Identifier: Apache-2.0 */
#ifndef SMALLVM_ERBIUM_PLATFORM_H
#define SMALLVM_ERBIUM_PLATFORM_H
#include <stdint.h>

/* Raw diagnostics only exist in selftest images; normal firmware is binary-only. */
void smallvm_platform_diag(const char *text);
void smallvm_platform_start_protocol(void);
void smallvm_platform_flush(void);
void delay(unsigned long msecs);
#ifdef SMALLVM_SELFTEST
int smallvm_platform_timer_selftest(void);
#endif
void smallvm_fault(uintptr_t cause, uintptr_t pc, uintptr_t value, uintptr_t sp)
    __attribute__((noreturn));
#endif
