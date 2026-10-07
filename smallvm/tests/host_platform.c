/* SPDX-License-Identifier: Apache-2.0 */
/* Deterministic host platform: real VM, simulated monotonic clock, no peripherals. */
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include "mem.h"
#include "interp.h"

static uint64 clock_us;
static unsigned long sent;
static const char *expected_panic;
static unsigned char panic_bytes[512];
static size_t panic_count;
void test_report(const char *line) { puts(line); fflush(stdout); }
uint64 totalMicrosecs(void) { clock_us += 50; return clock_us; }
uint32 microsecs(void) { return (uint32)totalMicrosecs(); }
uint32 millisecs(void) { return (uint32)(totalMicrosecs()/1000); }
void handleMicosecondClockWrap(void) { }
void delay(unsigned long ms) { clock_us += (uint64)ms*1000; }
void lightSleep(int ms) { if (ms > 0) delay((unsigned long)ms); }
void deepSleep(int secs) { if (secs > 0) clock_us += (uint64)secs*1000000; }
int recvBytes(uint8 *buf, int count) { (void)buf; (void)count; return 0; }
int sendBytes(uint8 *buf, int start, int end) {
 (void)buf;
 if (end < start) abort();
 /* Deliberately partial writes exercise VM output-ring advancement. */
 int n = end-start; if (n > 7) n = 7; sent += n;
 if (expected_panic) {
  if (panic_count+(size_t)n > sizeof(panic_bytes)) abort();
  memcpy(panic_bytes+panic_count, buf+start, (size_t)n); panic_count += n;
  size_t len = strlen(expected_panic);
  for (size_t i = 0; i+len <= panic_count; i++) {
   if (!memcmp(panic_bytes+i, expected_panic, len)) {
    puts("PASS invalid-object-address-rejected"); exit(0);
   }
  }
 }
 return n;
}
void restartSerial(void) { }
const char *boardType(void) { return "Erbium-host-test"; }
void hardwareInit(void) { }
extern int smallvm_selftest(void);
extern int smallvm_native_callback_test(PrimitiveFunction callback);
extern uint32 lastRcvTime;
int main(int argc, char **argv) {
 if (argc == 2) {
  uintptr_t addr;
  if (!strcmp(argv[1], "--reject-high-object") && sizeof(void *) > 4)
   addr = (uintptr_t)UINT64_C(0x100000000);
  else if (!strcmp(argv[1], "--reject-unaligned-object")) addr = 0x40000003;
  else return 2;
  expected_panic = "Object address must be aligned and below 4GB";
  lastRcvTime = 1; /* panic output is protocol-framed and requires an IDE session */
  memInit(); (void)ptrToObj((void *)addr);
  fputs("FAIL invalid object address accepted\n", stderr); return 1;
 }
 printf("HOST_DATA_MODEL int=%zu pointer=%zu OBJ=%zu\n", sizeof(int), sizeof(void *), sizeof(OBJ));
 int rc = smallvm_selftest();
 const char *library = getenv("SMALLVM_NATIVE_LIBRARY");
 if (library) {
  void *dl = dlopen(library, RTLD_NOW);
  if (!dl) { fprintf(stderr, "dlopen: %s\n", dlerror()); return 1; }
  PrimitiveFunction callback = (PrimitiveFunction)dlsym(dl, "native_test_primitive");
  if (!callback || (sizeof(void *) > 4 && (uintptr_t)callback <= UINT32_MAX)) {
   fprintf(stderr, "Expected a native function pointer above 4GiB\n"); return 1;
  }
  printf("NATIVE_CALLBACK address=%p\n", (void *)callback);
  rc |= smallvm_native_callback_test(callback);
  dlclose(dl);
 }
 printf("HOST_TRANSPORT accepted=%lu\n", sent);
 return rc;
}
