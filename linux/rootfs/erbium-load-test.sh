#!/bin/sh
# In-guest ELF loader test. Requires the real backend, initially held.
# /init handles erbium.loadtest and prints ERBIUM-LOAD-TEST-RESULT separately.
# No hold subcommand, status-output parsing, UART or stty is required.
fail() { echo "FAIL: loader: $*"; exit 1; }
ELF=${LOAD_TEST_ELF:-/firmware/loader-smoke.elf}
MTD=${LOAD_TEST_MTD:-/dev/mtd0}
MB0=0x40000068
MB1=0x40000070
SIG=4c44534d
PASS=600d600d
BSS_HEAD=0x00015000
BSS_TAIL=0x00016018

read_eq() {
    got=$(erbctl mem32 "$1") || fail "read $1"
    [ "$got" = "$2" ] || fail "$1: expected $2, got $got ($3)"
}
write32() { erbctl mem32 "$1" "$2" >/dev/null || fail "write $1"; }
guard_markers() {
    write32 "$MB0" 0x11223344
    write32 "$MB1" 0x55667788
}
guard_unchanged() {
    # Give an accidentally restarted hart time to publish PASS or FAIL.
    sleep 0.1
    read_eq "$MB0" 11223344 "$1 changed signature/reset state"
    read_eq "$MB1" 55667788 "$1 changed result/reset state"
}
still_held() {
    read_eq 0x40000028 00000006 "SoftReset warm-reset hold"
    read_eq 0x80f40240 000000ff "all thread-0 harts disabled"
    read_eq 0x80f40010 000000ff "all thread-1 harts disabled"
}
still_running() {
    # Warm reset need not clear mailboxes; inspect the reset and masks too.
    read_eq 0x40000028 00000004 "$1 asserted reset"
    read_eq 0x80f40240 000000fe "$1 changed thread-0 mask"
    read_eq 0x80f40010 000000ff "$1 changed thread-1 mask"
    read_eq 0x80d00018 40010040 "$1 changed boot PC"
}
zero_tail() {
    read_eq 0x00014008 00000000 "zero-filled gap after p_filesz"
    read_eq "$BSS_HEAD" 00000000 "BSS head"
    read_eq "$BSS_TAIL" 00000000 "BSS tail"
}
dirty_tail() {
    read_eq "$BSS_HEAD" a5a5a5a5 "firmware did not dirty BSS"
    read_eq "$BSS_TAIL" a5a5a5a5 "firmware did not dirty BSS tail"
}
image_readback() {
    read_eq 0x00011400 5eedc0de "text tail beyond 4 KiB"
    read_eq 0x00013000 13579bdf "initialized data"
    read_eq 0x00014000 2468ace0 "data tail beyond 4 KiB"
}
wait_pass() {
    n=0
    while [ "$n" -lt 100 ]; do
        result=$(erbctl mem32 "$MB1") || fail "poll result"
        if [ "$result" = "$PASS" ]; then
            read_eq "$MB0" "$SIG" "success signature"
            dirty_tail
            return
        fi
        case "$result" in
            bad000*) fail "firmware failure $result (1=data, 2=BSS, 3=text, 4=entry)" ;;
        esac
        n=$((n + 1))
        sleep 0.1
    done
    fail "timeout waiting for firmware: result=$result"
}
reject_without_changes() {
    guard_markers
    if erbctl load "$1" --mtd "$MTD" --check; then
        fail "invalid ELF accepted by --check: $1"
    fi
    guard_unchanged "invalid --check"
    still_running "invalid --check"
    dirty_tail
    if erbctl load "$1" --mtd "$MTD" --start; then
        fail "invalid ELF accepted with --start: $1"
    fi
    guard_unchanged "invalid --start"
    still_running "invalid --start"
    dirty_tail
}

[ -c "$MTD" ] || fail "no $MTD"
[ -c /dev/erbium0 ] || fail "no /dev/erbium0"
[ -s "$ELF" ] || fail "missing $ELF"
grep -q erbium.backend /proc/cmdline || fail "real erbium_emu backend required"

echo "== loader: valid --check is non-destructive"
guard_markers
erbctl load "$ELF" --mtd "$MTD" --check || fail "valid --check"
guard_unchanged "valid --check"

echo "== loader: default verified load leaves CPUs held"
# Pre-dirty both BSS endpoints so this also tests clearing on the first load.
write32 "$BSS_HEAD" 0xa5a5a5a5
write32 "$BSS_TAIL" 0xa5a5a5a5
erbctl load "$ELF" --mtd "$MTD" || fail "default load"
still_held
read_eq 0x80d00018 40010040 "e_entry programmed while held"
image_readback
zero_tail
guard_markers
guard_unchanged "held default load"
still_held
zero_tail

echo "== loader: --start uses e_entry and firmware validates data/BSS"
write32 "$MB0" 0
write32 "$MB1" 0
erbctl load "$ELF" --mtd "$MTD" --verify --start || fail "first start"
wait_pass

echo "== loader: reload clears BSS and remains held"
erbctl load "$ELF" --mtd "$MTD" --verify || fail "repeat held load"
still_held
image_readback
zero_tail
guard_markers
guard_unchanged "repeat held load"
still_held
zero_tail
write32 "$MB0" 0
write32 "$MB1" 0
erbctl load "$ELF" --mtd "$MTD" --start || fail "second start"
wait_pass

echo "== loader: check/rejected inputs do not reset or start a running hart"
# The firmware remains in a persistent loop with dirty BSS. A reset/restart
# would either clear the guard mailboxes or publish the BSS failure marker.
guard_markers
erbctl load "$ELF" --mtd "$MTD" --check || fail "running valid --check"
guard_unchanged "running valid --check"
still_running "running valid --check"
dirty_tail
# Create malformed fixtures in the guest; no Python or extra packaged ELF.
cp "$ELF" /tmp/loader-smoke-bad-magic.elf || fail "copy bad ELF"
printf '\000' | dd of=/tmp/loader-smoke-bad-magic.elf bs=1 count=1 conv=notrunc 2>/dev/null ||
    fail "corrupt ELF magic"
dd if="$ELF" of=/tmp/loader-smoke-truncated.elf bs=1 count=64 2>/dev/null ||
    fail "truncate ELF program headers"
reject_without_changes /tmp/loader-smoke-bad-magic.elf
reject_without_changes /tmp/loader-smoke-truncated.elf
# /proc is read-only to creation; this path cannot accidentally exist in /tmp.
reject_without_changes /proc/erbium-load-test-missing.elf

echo "== loader: MTD open failure never starts loaded code"
# Do not assume an I/O failure preserves mailbox/reset state: establish the
# guard after the command and prove no code is running by dirty BSS readback.
erbctl load "$ELF" --mtd "$MTD" --verify || fail "hold before I/O error"
zero_tail
if erbctl load "$ELF" --mtd /proc/erbium-load-test-missing-mtd --start; then
    fail "nonexistent MTD accepted"
fi
guard_markers
guard_unchanged "failed MTD load"
still_held
zero_tail

echo "ALL LOADER TESTS PASSED"
