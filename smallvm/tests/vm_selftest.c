/* SPDX-License-Identifier: Apache-2.0 */
/* Shared host/RV64 regression suite. Tests use the real VM and RAM code store. */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "mem.h"
#include "interp.h"
#include "persist.h"

extern void test_report(const char *line);
extern void runTasksUntilDone(void);
extern void startTaskForChunk(uint8 chunkIndex);
extern OBJ primNewByteArray(int argc, OBJ *args);
extern OBJ primListAddLast(int argc, OBJ *args);
extern OBJ primBoardType(void);
extern OBJ primHexToInt(int argc, OBJ *args);
extern OBJ primBinaryToInt(int argc, OBJ *args);
extern OBJ lastBroadcast;
extern int smallvm_gc_regressions(void);
extern int smallvm_gc_regression_check_count(void);
static int checks, failures;

#define CHECK(expr) do { checks++; if (!(expr)) { \
 char line[200]; snprintf(line, sizeof(line), "FAIL line %d: %s", __LINE__, #expr); \
 test_report(line); failures++; return; } } while (0)

static void clean_heap(void) {
 initTasks(); tempGCRoot = falseObj; fail(0); memClear();
}

static void test_layout_and_tags(void) {
 CHECK(sizeof(OBJ) == 4);
 CHECK(sizeof(int) == 4);
 static const int values[] = {-1073741824, -8388609, -65, -1, 0, 1, 63, 64, 8388608, 1073741823};
 for (unsigned i = 0; i < sizeof(values)/sizeof(values[0]); i++) {
  OBJ value = int2obj(values[i]); CHECK(isInt(value)); CHECK(obj2int(value) == values[i]);
 }
 CHECK(!isInt(falseObj)); CHECK(!isInt(trueObj)); CHECK(falseObj != trueObj);
 clean_heap();
 vars[0] = newObj(ListType, 3, zeroObj);
 CHECK(vars[0] != falseObj);
 CHECK((char *)&FIELD(vars[0], 0) - (char *)objToPtr(vars[0]) == 4);
 CHECK(WORDS(vars[0]) == 3); CHECK(TYPE(vars[0]) == ListType);
 FIELD(vars[0], 0) = int2obj(2); FIELD(vars[0], 1) = int2obj(-42);
 CHECK(obj2int(FIELD(vars[0], 1)) == -42);
 CHECK(ptrToObj(objToPtr(vars[0])) == vars[0]);
 test_report("PASS layout-and-tags");
}

static void test_strings_and_bytes(void) {
 clean_heap();
 const char utf8[] = "A\xc3\xa9\xf0\x9f\x98\x80";
 vars[0] = newStringFromBytes(utf8, sizeof(utf8)-1);
 CHECK(IS_TYPE(vars[0], StringType)); CHECK(!strcmp(obj2str(vars[0]), utf8));
 CHECK((char *)obj2str(vars[0]) - (char *)objToPtr(vars[0]) == 4);
 OBJ args[2] = {int2obj(2), vars[0]};
 vars[1] = primAt(2, args);
 CHECK(!strcmp(obj2str(vars[1]), "\xc3\xa9"));
 for (int n = 0; n <= 9; n++) {
  args[0] = int2obj(n); args[1] = int2obj(165);
  vars[2] = primNewByteArray(2, args);
  CHECK(IS_TYPE(vars[2], ByteArrayType)); CHECK(BYTES(vars[2]) == n);
  unsigned char *body = (unsigned char *)&FIELD(vars[2], 0);
  for (int j = 0; j < n; j++) CHECK(body[j] == 165);
  if (n) {
   OBJ put[3] = {int2obj(n), vars[2], int2obj(255)};
   primAtPut(3, put); CHECK(!failure());
   CHECK(obj2int(primAt(2, put)) == 255);
  }
 }
 vars[3] = primBoardType(); CHECK(IS_TYPE(vars[3], StringType));
 CHECK(!strcmp(obj2str(vars[3]), boardType()));
 gc(); CHECK(!strcmp(obj2str(vars[3]), boardType()));
 test_report("PASS strings-bytes-static-objects");
}

static void test_gc_and_resize(void) {
 clean_heap();
 int initial = wordsFree();
 (void)newString(100); /* deliberately dead object preceding live roots */
 vars[0] = newObj(ListType, 4, zeroObj); FIELD(vars[0], 0) = int2obj(3);
 vars[1] = newStringFromBytes("moving child", 12);
 FIELD(vars[0], 1) = vars[1]; FIELD(vars[0], 2) = vars[0]; /* cycle */
 vars[2] = newObj(ListType, 2, zeroObj); FIELD(vars[2], 0) = int2obj(1);
 FIELD(vars[2], 1) = vars[0]; FIELD(vars[0], 3) = vars[2]; /* nested cycle */
 tasks[0].status = running; tasks[0].sp = 1; tasks[0].stack[0] = vars[1]; taskCount = 1;
 lastBroadcast = vars[1]; tempGCRoot = vars[2];
 OBJ old = vars[0]; gc();
 CHECK(vars[0] != old); CHECK(FIELD(vars[0], 2) == vars[0]);
 CHECK(FIELD(vars[2], 1) == vars[0]); CHECK(FIELD(vars[0], 3) == vars[2]);
 CHECK(FIELD(vars[0], 1) == vars[1]); CHECK(!strcmp(obj2str(vars[1]), "moving child"));
 CHECK(tasks[0].stack[0] == vars[1]); CHECK(lastBroadcast == vars[1]); CHECK(tempGCRoot == vars[2]);
 OBJ grown = resizeObj(vars[0], 12);
 CHECK(vars[0] == grown); CHECK(WORDS(grown) == 12); CHECK(FIELD(grown, 2) == grown);
 CHECK(FIELD(vars[2], 1) == grown); CHECK(FIELD(grown, 1) == vars[1]);
 for (int i = 0; i < 1200; i++) {
  (void)newString(80); /* enough allocation to force multiple automatic collections */
  if (!(i % 101)) gc();
 }
 CHECK(!failure()); CHECK(FIELD(vars[0], 2) == vars[0]);
 CHECK(!strcmp(obj2str(FIELD(vars[0], 1)), "moving child"));
 clean_heap(); gc(); CHECK(wordsFree() == initial);
 test_report("PASS moving-gc-cycles-roots-resize");
}

static void test_list_growth(void) {
 clean_heap(); OBJ arg = int2obj(0); vars[0] = primNewList(1, &arg);
 for (int i = 0; i < 100; i++) {
  /* root primitive arguments just as the interpreter does on a task stack */
  tasks[0].status = running; tasks[0].sp = 2; taskCount = 1;
  tasks[0].stack[0] = int2obj(i); tasks[0].stack[1] = vars[0];
  primListAddLast(2, tasks[0].stack); CHECK(!failure());
  if (!(i % 7)) gc();
 }
 CHECK(obj2int(FIELD(vars[0], 0)) == 100);
 for (int i = 0; i < 100; i++) CHECK(obj2int(FIELD(vars[0], i+1)) == i);
 test_report("PASS list-growth-and-forwarding");
}

/* Real packed bytecodes, including code-resident literal objects. */
typedef struct { uint16 words[256]; int count; } Program;
static void emit(Program *p, int op, int arg) { p->words[p->count++] = (uint16)OP(op, arg); }
static void huge(Program *p, int value) {
 uint32 tag = (uint32)(uintptr_t)int2obj(value);
 emit(p, 4, 0); p->words[p->count++] = (uint16)tag; p->words[p->count++] = (uint16)(tag >> 16);
}
static int literal(Program *p, const char *s) {
 if (p->count & 1) p->words[p->count++] = 0;
 int at = p->count, bytes = (int)strlen(s)+1, words = (bytes+3)/4;
 uint32 header = HEADER(StringType, words);
 memcpy(&p->words[p->count], &header, 4); p->count += 2;
 memset(&p->words[p->count], 0, words*4);
 memcpy(&p->words[p->count], s, bytes); p->count += words*2;
 return at;
}
static int *install(Program *p, int id, int kind) {
 if (p->count & 1) p->words[p->count++] = 0;
 return appendPersistentRecord(chunkCode, id, kind, p->count*2, (uint8 *)p->words);
}

static void test_bytecode_and_persistence(void) {
 clean_heap(); clearPersistentMemory();
 Program caller = {{0}, 0}, callee = {{0}, 0};
 emit(&caller, 2, 13); emit(&caller, 2, 15); /* tagged 6, 7 */
 emit(&caller, 34, 0); caller.words[caller.count++] = (1 << 8) | 2;
 emit(&caller, 7, 0);
 huge(&caller, -1073741824); emit(&caller, 7, 1);
 huge(&caller, 1073741823); emit(&caller, 7, 2);
 uint32 medium = (uint32)(uintptr_t)int2obj(-123456);
 emit(&caller, 3, medium & 255); caller.words[caller.count++] = (uint16)(medium >> 8);
 emit(&caller, 7, 3);
 emit(&caller, 5, 0); int strRef = caller.count++; emit(&caller, 7, 4);
 emit(&caller, 79, 0); emit(&caller, 7, 5);
 huge(&caller, 9); huge(&caller, 165);
 emit(&caller, 37, 2); int primRef = caller.count++; emit(&caller, 7, 6);
 huge(&caller, 100); emit(&caller, 32, 1); /* sleep/yield and resume */
 emit(&caller, 0, 0);
 caller.words[strRef] = (uint16)(literal(&caller, "code literal") - strRef);
 caller.words[primRef] = (uint16)((DataPrims << 10) | (literal(&caller, "newByteArray") - primRef));
 emit(&callee, 13, 0); emit(&callee, 13, 1); emit(&callee, 52, 2); emit(&callee, 35, 1);
 CHECK(install(&caller, 0, startHat) != NULL); CHECK(install(&callee, 1, functionHat) != NULL);
 uint8 name[8] = {'a','n','s','w','e','r',0,0};
 CHECK(appendPersistentRecord(varName, 0, 0, sizeof(name), name) != NULL);
 restoreScripts(); CHECK(chunks[0].code != NULL); CHECK(chunks[1].chunkType == functionHat);
 CHECK(indexOfVarNamed("answer") == 0);
 startAll(); runTasksUntilDone(); CHECK(!failure());
 CHECK(obj2int(vars[0]) == 42); CHECK(obj2int(vars[1]) == -1073741824);
 CHECK(obj2int(vars[2]) == 1073741823); CHECK(obj2int(vars[3]) == -123456);
 CHECK(!strcmp(obj2str(vars[4]), "code literal")); CHECK(!strcmp(obj2str(vars[5]), boardType()));
 CHECK(BYTES(vars[6]) == 9); CHECK(*((uint8 *)&FIELD(vars[6], 0)) == 165);
 gc(); CHECK(!strcmp(obj2str(vars[4]), "code literal"));
 /* Compact stopped code, then reconstruct chunk table and execute again. */
 initTasks(); memClear();
 CHECK(install(&caller, 0, startHat) != NULL); /* obsolete record should be removed */
 restoreScripts();
 int usedBefore = 4, usedAfter, total;
 /* getCodeStore() compacts as a side effect, so count records directly. */
 for (int *p = recordAfter(NULL); p; p = recordAfter(p)) usedBefore += 4*(2+p[1]);
 compactCodeStore(&usedAfter, &total); CHECK(usedAfter < usedBefore); CHECK(total > usedAfter);
 restoreScripts(); startAll(); runTasksUntilDone(); CHECK(obj2int(vars[0]) == 42);
 CHECK(!strcmp(obj2str(vars[4]), "code literal"));
 initTasks(); memClear();
 CHECK(appendPersistentRecord(chunkDeleted, 0, 0, 0, NULL) != NULL);
 restoreScripts(); CHECK(chunks[0].code == NULL); CHECK(chunks[1].code != NULL);
 test_report("PASS bytecode-functions-literals-waits-ram-codestore");
}

static void test_conversions_and_errors(void) {
 clean_heap();
 OBJ arg = newStringFromBytes("-40000000", 9);
 CHECK(obj2int(primHexToInt(1, &arg)) == -1073741824); CHECK(!failure());
 arg = newStringFromBytes("3fffffff", 8);
 CHECK(obj2int(primHexToInt(1, &arg)) == 1073741823); CHECK(!failure());
 arg = newStringFromBytes("100000000", 9);
 (void)primHexToInt(1, &arg); CHECK(failure()); fail(0);
 arg = newStringFromBytes("-101010", 7);
 CHECK(obj2int(primBinaryToInt(1, &arg)) == -42); CHECK(!failure());
 (void)doPrimitiveCall(NetPrims, "absent", 0, NULL);
 CHECK(failure()); fail(0);
 test_report("PASS conversions-and-unsupported-primitives");
}

static void test_record_and_allocation_bounds(void) {
 clean_heap(); clearPersistentMemory();
 /* A three-byte source must not be read as a four-byte word under ASan. */
 uint8 payload[3] = {0x31, 0x32, 0x33};
 int *record = appendPersistentRecord(chunkAttribute, 0, 0, 3, payload);
 CHECK(record != NULL); CHECK(record[1] == 1);
 CHECK(!memcmp(record+2, payload, 3)); CHECK(((uint8 *)(record+2))[3] == 0);
 CHECK(recordAfter(NULL) == record); CHECK(recordAfter(record) == NULL);
 CHECK(appendPersistentRecord(chunkCode, 0, 0, -1, payload) == NULL);
 CHECK(appendPersistentRecord(chunkCode, 0, 0, 0x7fffffff, payload) == NULL);
 CHECK(appendPersistentRecord(chunkCode, 0, 0, 1, NULL) == NULL);
 record[1] = 0x7fffffff; CHECK(recordAfter(NULL) == NULL);
 restoreScripts(); CHECK(chunks[0].code == NULL);
 clearPersistentMemory();
 CHECK(newObj(ListType, -1, zeroObj) == falseObj); CHECK(failure()); fail(0);
 CHECK(newObj(ListType, 65536, zeroObj) == falseObj); CHECK(failure()); fail(0);
 test_report("PASS record-padding-corruption-and-allocation-bounds");
}

/* Host calls this with an actual >4GiB shared-library function pointer. */
int smallvm_native_callback_test(PrimitiveFunction callback) {
 clean_heap(); clearPersistentMemory();
 PrimEntry entry = {"native", callback};
 addPrimitiveSet(CameraPrims, "test", 1, &entry);
 Program code = {{0}, 0};
 emit(&code, 5, 0); int name1 = code.count++;
 emit(&code, 6, 20); emit(&code, 39, 2); emit(&code, 7, 7);
 emit(&code, 5, 0); int name2 = code.count++;
 emit(&code, 39, 1); emit(&code, 7, 8); emit(&code, 0, 0);
 int name = literal(&code, "[test:native]");
 code.words[name1] = (uint16)(name-name1); code.words[name2] = (uint16)(name-name2);
 if (!install(&code, 0, command)) return 1;
 restoreScripts();
 vars[20] = newObj(ListType, 3, zeroObj);
 FIELD(vars[20], 0) = int2obj(2);
 FIELD(vars[20], 1) = int2obj(20); FIELD(vars[20], 2) = int2obj(22);
 startTaskForChunk(0); runTasksUntilDone();
 primsInit(); /* unregister the stack-owned entry before returning */
 if (failure() || obj2int(vars[7]) != 42 || obj2int(vars[8]) != 42) return 1;
 test_report("PASS native-width-named-callback-zero-and-two-args");
 return 0;
}

int smallvm_selftest(void) {
 checks = failures = 0; memInit(); primsInit(); restoreScripts();
 test_layout_and_tags(); test_strings_and_bytes(); test_gc_and_resize();
 test_list_growth(); test_bytecode_and_persistence(); test_conversions_and_errors();
 test_record_and_allocation_bounds();
 failures += smallvm_gc_regressions();
 checks += smallvm_gc_regression_check_count();
 initTasks(); memClear(); fail(0);
 char line[100]; snprintf(line, sizeof(line), "SMALLVM_SELFTEST %s checks=%d failures=%d", failures ? "FAIL" : "PASS", checks, failures);
 test_report(line); return failures ? 1 : 0;
}
