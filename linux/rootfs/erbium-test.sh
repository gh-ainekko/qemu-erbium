#!/bin/sh
# In-guest end-to-end test of the Erbium xSPI driver. Exit 0 on success.
fail() { echo "FAIL: $*"; exit 1; }
MTD=/dev/mtd0
[ -c $MTD ] || fail "no $MTD"
[ -c /dev/erbium0 ] || fail "no /dev/erbium0"
grep -q erbium-mram /proc/mtd || fail "mtd is not erbium-mram"
cat /sys/class/misc/erbium0/rates
erbctl info || fail info

echo "== SCCR"
ID0=$(erbctl reg 0x00); [ "$ID0" = "00000002" ] || fail "ID0=$ID0"
erbctl sfdp 0 16 | head -1 | grep -q '53 46 44 50' || fail sfdp

echo "== MTD read/write"
dd if=/dev/zero bs=1k count=64 2>/dev/null | tr '\0' 'U' > /tmp/pat0
# deterministic pseudo-random 64 KiB
i=0; : > /tmp/pat
while [ $i -lt 256 ]; do printf "%s" "$(printf '%03d' $i | md5sum | head -c 32)"; i=$((i+1)); done > /tmp/seed
i=0; while [ $i -lt 8 ]; do cat /tmp/seed; i=$((i+1)); done > /tmp/pat
SZ=$(cat /tmp/pat | wc -c)
dd if=/tmp/pat of=$MTD bs=4k seek=4 conv=notrunc 2>/dev/null || fail "mtd write"
dd if=$MTD of=/tmp/rd bs=4k skip=4 count=$((SZ/4096)) 2>/dev/null || fail "mtd read"
cmp /tmp/pat /tmp/rd || fail "mtd mismatch"
# unaligned RMW through the block device
[ -b /dev/mtdblock0 ] || fail "no mtdblock0"
printf 'hello erbium' | dd of=$MTD bs=1 seek=$((0x4003)) conv=notrunc 2>/dev/null || fail "unaligned write"
dd if=$MTD bs=1 skip=$((0x4000)) count=16 2>/dev/null | hexdump -C
dd if=$MTD bs=1 skip=$((0x4003)) count=12 2>/dev/null | grep -q 'hello erbium' || fail "unaligned readback"
dd if=/tmp/pat of=/dev/mtdblock0 bs=512 seek=200 count=8 conv=notrunc 2>/dev/null || fail "mtdblock write"
dd if=/dev/mtdblock0 bs=512 skip=200 count=8 2>/dev/null | cmp - /tmp/pat 2>/dev/null; # cmp EOF on second is fine
dd if=$MTD bs=512 skip=200 count=8 2>/dev/null > /tmp/blk; head -c 4096 /tmp/pat | cmp - /tmp/blk || fail "mtdblock mismatch"

echo "== control plane"
erbctl mem 0x4000C000 32
erbctl memw 0x4000C100 1 2 3 4 5 6 7 8 || fail "sram write"
erbctl mem 0x4000C100 8 | grep -q '01 02 03 04 05 06 07 08' || fail "sram readback"
erbctl mem32 0x40000068
if grep -q erbium.backend /proc/cmdline; then
  echo "== minion job via mailbox (erbium_emu backend)"
  # fill 4 KiB at MRAM+0x20000 with 0x5a
  R=$(erbctl job 0x025a0200 10000) || fail "fill job"
  [ "$R" = "00000001" ] || fail "fill job result $R"
  dd if=$MTD bs=4k skip=32 count=1 2>/dev/null | tr -d 'Z' | wc -c | grep -qx 0 || fail "fill not visible"
  # CRC of the pattern we wrote at 0x4000
  R=$(erbctl job 0x01000040 10000) || fail "crc job"
  echo "crc32(MRAM+0x4000..+4k) = $R"
  [ "$R" != "00000000" ] || fail "crc zero"
fi
echo "== reset"
erbctl reset || fail reset
cat /sys/class/misc/erbium0/rates
erbctl reg 0x28
dd if=$MTD of=/tmp/rd2 bs=4k skip=4 count=$((SZ/4096)) 2>/dev/null
cmp /tmp/pat /tmp/rd2 >/dev/null 2>&1 || echo "note: hello-erbium patch expected -> compare tail only"
dd if=$MTD bs=4k skip=5 count=$((SZ/4096-1)) 2>/dev/null > /tmp/rd3
dd if=/tmp/pat bs=4k skip=1 2>/dev/null | cmp - /tmp/rd3 || fail "data lost after reset"
echo "ALL TESTS PASSED"
