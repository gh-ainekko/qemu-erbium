/* SPDX-License-Identifier: Apache-2.0 */
/* Moving-GC and code-literal regressions shared by host, RV64 and legacy OBJ.
 * The caller initializes the VM and adds our returned failures to its summary.
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "mem.h"
#include "interp.h"
#include "persist.h"

extern void test_report(const char *line);
extern void startTaskForChunk(uint8 chunkIndex);
extern void runTasksUntilDone(void);
extern OBJ charAt(OBJ stringObj, int index);
extern OBJ primAsByteArray(int argc, OBJ *args);
extern OBJ primSplit(int argc, OBJ *args);
extern PrimitiveFunction findPrimitive(char *name);
extern OBJ lastBroadcast;

static int checks, failures;

#define CHECK(condition) do { \
    checks++; \
    if (!(condition)) { \
        char line[200]; \
        snprintf(line, sizeof(line), "FAIL gc-regression line %d: %s", \
            __LINE__, #condition); \
        test_report(line); \
        failures++; \
        return; \
    } \
} while (0)

static void clean_heap(void) {
    initTasks();
    memClear();
    fail(0);
}

static void root_arguments(int count) {
    tasks[0].status = running;
    tasks[0].sp = count;
    taskCount = 1;
}

/* Consume space with garbage, leaving exactly reserve allocatable words.
 * The dummy is deliberately binary: its fill is raw bits, not a GC root.
 * Callers also leave garbage BEFORE their source so collection must move it.
 */
static int leave_free(int reserve) {
    int count = wordsFree() - reserve - 2;
    if (count < 0) return 0;
    if (!newObj(ByteArrayType, count, falseObj)) return 0;
    return !failure() && wordsFree() == reserve;
}

static int string_equals(OBJ obj, const char *text) {
    return IS_TYPE(obj, StringType) && !strcmp(obj2str(obj), text);
}

static void test_character(int use_char_at, int unicode) {
    const char *source = unicode ? "A\xc3\xa9\xf0\x9f\x98\x80Z" : "ABCDE";
    const char *expected = unicode ? "\xf0\x9f\x98\x80" : "A";
    int index = unicode ? 3 : 1;
    clean_heap();
    CHECK(newObj(StringType, 0, falseObj) != falseObj); /* dead prefix */
    vars[0] = newStringFromBytes(source, (int)strlen(source));
    CHECK(string_equals(vars[0], source));
    root_arguments(2);
    tasks[0].stack[0] = int2obj(index);
    tasks[0].stack[1] = vars[0];
    OBJ old_source = vars[0];
    CHECK(leave_free(0));
    vars[1] = use_char_at ? charAt(tasks[0].stack[1], index)
        : primAt(2, tasks[0].stack);
    CHECK(!failure());
    CHECK(vars[0] != old_source); /* collection happened inside the primitive */
    CHECK(tasks[0].stack[1] == vars[0]);
    CHECK(string_equals(vars[0], source));
    CHECK(string_equals(vars[1], expected));
    gc();
    CHECK(string_equals(vars[1], expected));
    test_report(use_char_at ? "PASS gc-charAt-forced-moving"
        : "PASS gc-At-forced-moving");
}

static void test_as_byte_array(void) {
    static const char source[] = "ABCDE\xc3\xa9\xf0\x9f\x98\x80";
    clean_heap();
    CHECK(newObj(StringType, 0, falseObj) != falseObj);
    vars[0] = newStringFromBytes(source, sizeof(source) - 1);
    root_arguments(1);
    tasks[0].stack[0] = vars[0];
    OBJ old_source = vars[0];
    CHECK(leave_free(0));
    vars[1] = primAsByteArray(1, tasks[0].stack);
    CHECK(!failure());
    CHECK(vars[0] != old_source);
    CHECK(IS_TYPE(vars[1], ByteArrayType));
    CHECK(BYTES(vars[1]) == (int)sizeof(source) - 1);
    CHECK(!memcmp(&FIELD(vars[1], 0), source, sizeof(source) - 1));
    gc();
    CHECK(!memcmp(&FIELD(vars[1], 0), source, sizeof(source) - 1));
    test_report("PASS gc-asByteArray-forced-moving");
}

static void test_split(int empty_delimiter, int later_collection) {
    static const char *characters[] = {"A", "\xc3\xa9", "\xf0\x9f\x98\x80"};
    static const char *pieces[] = {"alpha", "\xc3\xa9", "omega"};
    const char **expected = empty_delimiter ? characters : pieces;
    const char *source = empty_delimiter ? "A\xc3\xa9\xf0\x9f\x98\x80"
        : "alpha::\xc3\xa9::omega";
    const char *delimiter = empty_delimiter ? "" : "::";
    const int count = 3;
    clean_heap();
    CHECK(newObj(StringType, 0, falseObj) != falseObj);
    vars[0] = newStringFromBytes(source, (int)strlen(source));
    vars[1] = newStringFromBytes(delimiter, (int)strlen(delimiter));
    CHECK(string_equals(vars[0], source));
    CHECK(string_equals(vars[1], delimiter));
    root_arguments(3);
    tasks[0].stack[0] = vars[0];
    tasks[0].stack[1] = vars[1];
    tasks[0].stack[2] = falseObj; /* do not convert numeric substrings */
    OBJ old_source = vars[0], old_delimiter = vars[1];
    /* Enough room for the result list and first substring ONLY. The second
     * substring then collects with an already-populated result-list root.
     * With reserve=0, the initial result-list allocation collects instead.
     */
    int first_words = ((int)strlen(expected[0]) + 4) / 4;
    int reserve = later_collection ? count + 3 + first_words : 0;
    CHECK(leave_free(reserve));
    vars[2] = primSplit(3, tasks[0].stack);
    CHECK(!failure());
    CHECK(vars[0] != old_source);
    CHECK(vars[1] != old_delimiter);
    CHECK(tasks[0].stack[0] == vars[0]);
    CHECK(tasks[0].stack[1] == vars[1]);
    CHECK(string_equals(vars[0], source));
    CHECK(string_equals(vars[1], delimiter));
    CHECK(IS_TYPE(vars[2], ListType));
    CHECK(obj2int(FIELD(vars[2], 0)) == count);
    for (int i = 0; i < count; ++i)
        CHECK(string_equals(FIELD(vars[2], i + 1), expected[i]));
    tempGCRoot = falseObj;
    gc();
    for (int i = 0; i < count; ++i)
        CHECK(string_equals(FIELD(vars[2], i + 1), expected[i]));
    test_report(later_collection ? "PASS gc-split-later-allocation"
        : "PASS gc-split-initial-allocation");
}

static void test_allocation_fill(void) {
    clean_heap();
    CHECK(newObj(StringType, 0, falseObj) != falseObj);
    /* ONLY the allocator's internal fill root may keep this object alive.
     * No variable, Task argument, or tempGCRoot points to it.
     */
    OBJ fill = newStringFromBytes("fill survives", 13);
    CHECK(string_equals(fill, "fill survives"));
    CHECK(leave_free(0));
    vars[0] = newObj(ListType, 5, fill);
    CHECK(!failure());
    CHECK(IS_TYPE(vars[0], ListType));
    OBJ forwarded_fill = FIELD(vars[0], 1);
    CHECK(forwarded_fill != fill);
    CHECK(string_equals(forwarded_fill, "fill survives"));
    for (int i = 0; i < 5; ++i) CHECK(FIELD(vars[0], i) == forwarded_fill);
    FIELD(vars[0], 0) = int2obj(4);
    gc();
    for (int i = 1; i < 5; ++i)
        CHECK(string_equals(FIELD(vars[0], i), "fill survives"));

    /* Conversely, binary fills must NOT be treated as handles and forwarded,
     * even when the raw 32-bit pattern happens to equal a live heap address.
     */
    clean_heap();
    CHECK(newObj(StringType, 0, falseObj) != falseObj);
    vars[0] = newStringFromBytes("raw", 3);
    OBJ raw_fill = vars[0];
    CHECK(leave_free(0));
    vars[1] = newObj(ByteArrayType, 2, raw_fill);
    CHECK(!failure());
    CHECK(vars[0] != raw_fill);
    CHECK(IS_TYPE(vars[1], ByteArrayType));
    CHECK(objBits(FIELD(vars[1], 0)) == objBits(raw_fill));
    CHECK(objBits(FIELD(vars[1], 1)) == objBits(raw_fill));
    test_report("PASS gc-allocation-fill-root-and-raw-binary-fill");
}

static void test_resize_collects(void) {
    clean_heap();
    CHECK(newObj(StringType, 0, falseObj) != falseObj);
    vars[0] = newObj(ListType, 3, zeroObj);
    vars[1] = newObj(ListType, 2, zeroObj);
    vars[2] = newStringFromBytes("resize child", 12);
    CHECK(vars[0] && vars[1] && vars[2]);
    FIELD(vars[0], 0) = int2obj(2);
    FIELD(vars[0], 1) = vars[0]; /* self reference */
    FIELD(vars[0], 2) = vars[2];
    FIELD(vars[1], 0) = int2obj(1);
    FIELD(vars[1], 1) = vars[0]; /* incoming reference */
    root_arguments(2);
    tasks[0].stack[0] = vars[0];
    tasks[0].stack[1] = vars[2];
    lastBroadcast = vars[2];
    OBJ old_child = vars[2];
    CHECK(leave_free(0));
    OBJ grown = resizeObj(vars[0], 11);
    CHECK(!failure());
    CHECK(vars[2] != old_child); /* distinguishes internal GC from resize alone */
    CHECK(grown == vars[0]);
    CHECK(WORDS(grown) == 11);
    CHECK(FIELD(grown, 1) == grown);
    CHECK(FIELD(vars[1], 1) == grown);
    CHECK(tasks[0].stack[0] == grown);
    CHECK(FIELD(grown, 2) == vars[2]);
    CHECK(tasks[0].stack[1] == vars[2] && lastBroadcast == vars[2]);
    CHECK(string_equals(vars[2], "resize child"));
    for (int i = 3; i < 11; ++i) CHECK(FIELD(grown, i) == zeroObj);
    gc();
    CHECK(FIELD(vars[0], 1) == vars[0]);
    CHECK(FIELD(vars[1], 1) == vars[0]);
    test_report("PASS gc-resize-internal-collection-and-forwarding");
}

/* Build host/compiler-compatible 16-bit bytecode with a packed string literal.
 * Tests obtain OBJ references by EXECUTING pushLiteral, never by casting code.
 * Heap-owned literal policies are therefore tested without assuming addresses.
 */
typedef struct {
    uint16 words[64];
    int count;
} Program;

static void literal_program(Program *p, const char *text) {
    memset(p, 0, sizeof(*p));
    p->words[0] = (uint16)OP(5, 0); /* pushLiteral */
    p->words[1] = 5; /* offset from operand at halfword 1 to header at 6 */
    p->words[2] = (uint16)OP(7, 0); /* storeGlobal 0 */
    p->words[3] = (uint16)OP(0, 0); /* halt before literal data */
    int bytes = (int)strlen(text) + 1;
    int words = (bytes + 3) / 4;
    uint32 header = HEADER(StringType, words);
    memcpy(p->words + 6, &header, 4);
    memcpy(p->words + 8, text, (size_t)bytes);
    p->count = 8 + 2 * words;
}

static int *install(Program *p, int id) {
    return appendPersistentRecord(chunkCode, id, startHat,
        p->count * 2, (uint8 *)p->words);
}

static int used_code_words(void) {
    int result = 1;
    for (int *p = recordAfter(NULL); p; p = recordAfter(p))
        result += 2 + p[1];
    return result;
}

static void test_retained_literal(int automatic, int stopped) {
    clean_heap();
    clearPersistentMemory();
    Program source, replacement;
    literal_program(&source, "ABCDE");
    literal_program(&replacement, "VWXYZ");
    CHECK(install(&replacement, 2) != NULL); /* obsolete prefix to remove */
    CHECK(install(&source, 0) != NULL);
    CHECK(install(&replacement, 2) != NULL);
    restoreScripts();
    int *old_code = chunks[0].code;
    CHECK(old_code != NULL);
    startTaskForChunk(0);
    runTasksUntilDone();
    CHECK(!failure());
    CHECK(string_equals(vars[0], "ABCDE"));

    vars[1] = newObj(ListType, 2, zeroObj);
    CHECK(vars[1] != falseObj);
    FIELD(vars[1], 0) = int2obj(1);
    FIELD(vars[1], 1) = vars[0];
    lastBroadcast = vars[0];
    initTasks(); /* intentionally NOT memClear: stopped globals remain valid */
    if (!stopped) {
        root_arguments(1);
        tasks[0].status = waiting_micros;
        tasks[0].stack[0] = vars[0];
        tasks[0].taskChunkIndex = 0;
        tasks[0].currentChunkIndex = 0;
        tasks[0].code = chunks[0].code;
    }
    if (automatic) {
        uint8 garbage[256] = {0};
        int compacted = 0;
        int limit = codeStoreSize() / (int)sizeof(garbage) + 8;
        for (int i = 0; i < limit; ++i) {
            int before = used_code_words();
            CHECK(appendPersistentRecord(chunkDeleted, 7, 0,
                sizeof(garbage), garbage) != NULL);
            if (used_code_words() < before) {
                compacted = 1;
                break;
            }
        }
        CHECK(compacted);
    } else {
        int before = used_code_words(), used, total;
        compactCodeStore(&used, &total);
        CHECK(used < before * 4);
        CHECK(total > used);
    }
    CHECK(chunks[0].code != old_code); /* test truly moved the code record */
    CHECK(string_equals(vars[0], "ABCDE"));
    CHECK(string_equals(FIELD(vars[1], 1), "ABCDE"));
    CHECK(string_equals(lastBroadcast, "ABCDE"));
    CHECK(FIELD(vars[1], 1) == vars[0] && lastBroadcast == vars[0]);
    if (!stopped) {
        CHECK(tasks[0].code == chunks[0].code);
        CHECK(tasks[0].stack[0] == vars[0]);
    }
    gc(); /* heap-owned literals must also remain correctly rooted */
    CHECK(string_equals(vars[0], "ABCDE"));
    CHECK(FIELD(vars[1], 1) == vars[0] && lastBroadcast == vars[0]);
    if (!stopped) CHECK(tasks[0].stack[0] == vars[0]);
    test_report(automatic ? "PASS gc-literal-retained-automatic-compaction"
        : stopped ? "PASS gc-literal-retained-stopped-globals"
        : "PASS gc-literal-retained-live-compaction");
}

static void test_long_primitive_names(void) {
    static const int lengths[] = {99, 100, 150};
    char name[180];
    clean_heap();
    CHECK(findPrimitive("[data:newByteArray]") != NULL);
    for (unsigned i = 0; i < sizeof(lengths) / sizeof(lengths[0]); ++i) {
        int count = lengths[i];
        name[0] = '[';
        memset(name + 1, 'x', (size_t)count);
        memcpy(name + 1 + count, ":missing]", sizeof(":missing]"));
        CHECK(findPrimitive(name) == NULL);
        fail(0);
        memcpy(name, "[data:", 6);
        memset(name + 6, 'x', (size_t)count);
        name[6 + count] = ']';
        name[7 + count] = 0;
        CHECK(findPrimitive(name) == NULL);
        fail(0);
    }
    strcpy(name, "[:missing]");
    CHECK(findPrimitive(name) == NULL);
    strcpy(name, "[data:]");
    CHECK(findPrimitive(name) == NULL);
    strcpy(name, "[data without colon]");
    CHECK(findPrimitive(name) == NULL);
    strcpy(name, "not a primitive");
    CHECK(findPrimitive(name) == NULL);
    test_report("PASS gc-named-primitive-component-bounds");
}

int smallvm_gc_regression_check_count(void) { return checks; }

int smallvm_gc_regressions(void) {
    checks = failures = 0;
    test_character(0, 0);
    test_character(0, 1);
    test_character(1, 0);
    test_character(1, 1);
    test_as_byte_array();
    test_split(1, 0);
    test_split(0, 0);
    test_split(1, 1);
    test_split(0, 1);
    test_allocation_fill();
    test_resize_collects();
    test_retained_literal(0, 0);
    test_retained_literal(0, 1);
    test_retained_literal(1, 0);
    /* Last so an unfixed native stack overflow cannot hide earlier results. */
    test_long_primitive_names();
    clean_heap();
    clearPersistentMemory();
    restoreScripts();
    char line[120];
    snprintf(line, sizeof(line), "SMALLVM_GC_REGRESSIONS %s checks=%d failures=%d",
        failures ? "FAIL" : "PASS", checks, failures);
    test_report(line);
    return failures;
}
