# SPDX-License-Identifier: Apache-2.0
# Isolated Erbium/LP64 C target. Source fetching/patches are managed separately.
ROOT := $(abspath ..)
SMALLVM_SRC ?= $(ROOT)/ext/smallvm
BUILD ?= $(ROOT)/build/smallvm
DEPS ?= $(ROOT)/build/smallvm/deps
CROSS ?= riscv64-unknown-elf-
CC := $(CROSS)gcc
OBJDUMP := $(CROSS)objdump
READELF := $(CROSS)readelf
SIZE := $(CROSS)size
PICOLIBC := $(DEPS)/usr/lib/picolibc/riscv64-unknown-elf
LIBC := $(PICOLIBC)/lib/rv64im/lp64/libc.a
SELFTEST_SOURCES ?= tests/vm_selftest.c tests/gc_regressions.c
ERBIUM_TIMER_HZ ?= 2000000
ARCH := -march=rv64imc_zicsr_zifencei -mabi=lp64 -mcmodel=medany
CPPFLAGS := -I$(SMALLVM_SRC)/vm -Ierbium \
	-DSMALLVM_MINIMAL -DSMALLVM_COMPACT_32=1 -DSMALLVM_OBJSTORE_BYTES=64000
CFLAGS := -std=gnu11 -O2 -g -Wall -Wextra -Wno-unused-parameter \
	-Werror=implicit-function-declaration -Werror=pointer-to-int-cast -Werror=int-to-pointer-cast \
	-ffunction-sections -fdata-sections -fno-common -fno-strict-aliasing \
	-ffreestanding -fno-stack-protector
RVFLAGS := $(ARCH) -DERBIUM -DERBIUM_TIMER_HZ=$(ERBIUM_TIMER_HZ) \
	-isystem $(PICOLIBC)/include
VM := mem interp runtime persist dataPrims varPrims miscPrims tinyJSON boardHooks
SOURCES := $(addprefix $(SMALLVM_SRC)/vm/,$(addsuffix .c,$(VM))) \
	platform/stubs.c erbium/platform.c erbium/main.c erbium/startup.S
FWOBJ := $(addprefix $(BUILD)/firmware/,$(notdir $(SOURCES:.c=.o)))
FWOBJ := $(FWOBJ:.S=.o)
STOBJ := $(addprefix $(BUILD)/selftest/,$(notdir $(SOURCES:.c=.o) $(SELFTEST_SOURCES:.c=.o)))
STOBJ := $(STOBJ:.S=.o)
HEADERS := $(wildcard $(SMALLVM_SRC)/vm/*.h) erbium/platform.h
VPATH := $(SMALLVM_SRC)/vm:platform:erbium:$(sort $(dir $(SELFTEST_SOURCES)))
LDFLAGS := $(ARCH) -nostartfiles -nostdlib -T erbium/erbium.ld \
	-Wl,--gc-sections -Wl,--no-relax -Wl,--defsym=vfprintf=__i_vfprintf

.PHONY: all firmware selftest deps clean host-selftest host-selftest-sanitize host-selftest-32 FORCE
all: firmware
firmware: $(BUILD)/smallvm.elf $(BUILD)/smallvm.dis $(BUILD)/smallvm.headers
selftest: $(BUILD)/smallvm-selftest.elf $(BUILD)/smallvm-selftest.dis $(BUILD)/smallvm-selftest.headers
deps: $(DEPS)/picolibc.lock
$(DEPS)/picolibc.lock: $(ROOT)/scripts/build-smallvm.sh
	SMALLVM_DEPS="$(DEPS)" bash $(ROOT)/scripts/build-smallvm.sh --prepare-libc
$(LIBC): $(DEPS)/picolibc.lock
	@test -f $@ || { echo "Missing libc; remove $(DEPS)/picolibc.lock and run make deps"; exit 1; }
# Record build options so changing CROSS, source directory or timer calibration
# cannot silently reuse objects built with different flags.
FORCE:
$(BUILD)/rv64.flags: FORCE
	@mkdir -p $(@D)
	@printf '%s\n' '$(CC) $(CPPFLAGS) $(CFLAGS) $(RVFLAGS) $(LDFLAGS)' > $@.tmp
	@cmp -s $@.tmp $@ && rm -f $@.tmp || mv $@.tmp $@
$(BUILD)/host.flags: FORCE
	@mkdir -p $(@D)
	@printf '%s\n' '$(HOSTCC) $(CPPFLAGS) $(CFLAGS)' > $@.tmp
	@cmp -s $@.tmp $@ && rm -f $@.tmp || mv $@.tmp $@
$(BUILD)/firmware/%.o: %.c $(HEADERS) $(DEPS)/picolibc.lock $(BUILD)/rv64.flags Makefile
	@mkdir -p $(@D)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(RVFLAGS) -MMD -MP -c $< -o $@
$(BUILD)/selftest/%.o: %.c $(HEADERS) $(DEPS)/picolibc.lock $(BUILD)/rv64.flags Makefile
	@mkdir -p $(@D)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(RVFLAGS) -DSMALLVM_SELFTEST -MMD -MP -c $< -o $@
$(BUILD)/firmware/%.o: %.S $(DEPS)/picolibc.lock $(BUILD)/rv64.flags Makefile
	@mkdir -p $(@D)
	$(CC) $(ARCH) -g -c $< -o $@
$(BUILD)/selftest/%.o: %.S $(DEPS)/picolibc.lock $(BUILD)/rv64.flags Makefile
	@mkdir -p $(@D)
	$(CC) $(ARCH) -g -c $< -o $@
$(BUILD)/smallvm.elf: $(FWOBJ) $(LIBC) erbium/erbium.ld
	$(CC) $(LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(FWOBJ) $(LIBC) -lgcc
	$(SIZE) $@
$(BUILD)/smallvm-selftest.elf: $(STOBJ) $(LIBC) erbium/erbium.ld
	$(CC) $(LDFLAGS) -Wl,-Map,$(@:.elf=.map) -o $@ $(STOBJ) $(LIBC) -lgcc
	$(SIZE) $@
$(BUILD)/%.dis: $(BUILD)/%.elf
	$(OBJDUMP) -d $< > $@
$(BUILD)/%.headers: $(BUILD)/%.elf
	$(READELF) -h -l -A $< > $@

# Low-address, non-PIE native harness shares stubs/minimal profile with firmware.
# Parent-owned tests provide main(), timers, byte transport and board identity.
HOSTCC ?= cc
HOSTSOURCES := $(addprefix $(SMALLVM_SRC)/vm/,$(addsuffix .c,$(VM))) \
	platform/stubs.c $(SELFTEST_SOURCES) tests/host_platform.c
host-selftest: $(BUILD)/host-selftest $(BUILD)/native-primitive.so
	SMALLVM_NATIVE_LIBRARY=$(BUILD)/native-primitive.so timeout 60 $(BUILD)/host-selftest
	timeout 10 $(BUILD)/host-selftest --reject-high-object
	timeout 10 $(BUILD)/host-selftest --reject-unaligned-object
$(BUILD)/native-primitive.so: tests/native_primitive.c
	@mkdir -p $(@D)
	$(HOSTCC) -shared -fPIC -O2 -g -o $@ $<
$(BUILD)/host-selftest: $(HOSTSOURCES) $(HEADERS) $(BUILD)/host.flags Makefile
	@mkdir -p $(@D)
	$(HOSTCC) $(CPPFLAGS) $(CFLAGS) -fno-pie -no-pie -DSMALLVM_SELFTEST \
		-Wl,--gc-sections -o $@ $(HOSTSOURCES)
host-selftest-sanitize: $(BUILD)/host-selftest-sanitize $(BUILD)/native-primitive.so
	SMALLVM_NATIVE_LIBRARY=$(BUILD)/native-primitive.so \
		ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 \
		timeout 60 $(BUILD)/host-selftest-sanitize
$(BUILD)/host-selftest-sanitize: $(HOSTSOURCES) $(HEADERS) $(BUILD)/host.flags Makefile
	@mkdir -p $(@D)
	$(HOSTCC) $(CPPFLAGS) $(CFLAGS) -fno-pie -no-pie -DSMALLVM_SELFTEST \
		-fsanitize=address,undefined -fno-omit-frame-pointer \
		-Wl,--gc-sections -o $@ $(HOSTSOURCES)
# Optional legacy-pointer regression (requires 32-bit libc/GCC support and qemu-user).
HOST32_RUNNER ?= qemu-i386
host-selftest-32: $(BUILD)/host-selftest-32
	UBSAN_OPTIONS=halt_on_error=1 timeout 60 $(HOST32_RUNNER) $(BUILD)/host-selftest-32
$(BUILD)/host-selftest-32: $(HOSTSOURCES) $(HEADERS) $(BUILD)/host.flags Makefile
	@mkdir -p $(@D)
	$(HOSTCC) -m32 $(filter-out -DSMALLVM_COMPACT_32=1,$(CPPFLAGS)) \
		-DSMALLVM_COMPACT_32=0 $(CFLAGS) -fno-pie -no-pie -DSMALLVM_SELFTEST \
		-fsanitize=undefined -fno-sanitize-recover=all \
		-Wl,--gc-sections -o $@ $(HOSTSOURCES)
clean:
	rm -f $(FWOBJ) $(STOBJ) $(FWOBJ:.o=.d) $(STOBJ:.o=.d) \
		$(BUILD)/smallvm.elf $(BUILD)/smallvm.map $(BUILD)/smallvm.dis $(BUILD)/smallvm.headers \
		$(BUILD)/smallvm-selftest.elf $(BUILD)/smallvm-selftest.map \
		$(BUILD)/smallvm-selftest.dis $(BUILD)/smallvm-selftest.headers \
		$(BUILD)/host-selftest $(BUILD)/host-selftest-sanitize \
		$(BUILD)/host-selftest-32 $(BUILD)/native-primitive.so \
		$(BUILD)/rv64.flags $(BUILD)/host.flags
-include $(wildcard $(BUILD)/firmware/*.d $(BUILD)/selftest/*.d)
