#!/bin/sh
set -eu
[ "$(cat /mnt/host/input.txt)" = 'hello from host' ]
mode=ro
for arg in $(cat /proc/cmdline); do
    [ "$arg" != erbium.share=rw ] || mode=rw
done
if [ "$mode" = ro ]; then
    # Even guest root remounting rw must not bypass QEMU's readonly export.
    mount -o remount,rw /mnt/host 2>/dev/null || true
    if (echo forbidden > /mnt/host/output.txt) 2>/dev/null; then
        echo 'FAIL: read-only host export accepted a write' >&2
        exit 1
    fi
else
    printf 'hello from guest\n' > /mnt/host/output.txt
    printf ' appended by guest\n' >> /mnt/host/input.txt
    sync
    # Exercise pipe wakeups/9P reads with both guest CPUs present. Shared-folder
    # runners use single-thread TCG to avoid the observed MTTCG/SMP failure.
    pids=""
    for worker in 1 2 3 4; do
        (
            for iteration in $(seq 1 50); do
                [ "$(cat /mnt/host/live.txt)" = 'original on host' ] || exit 1
            done
        ) &
        pids="$pids $!"
    done
    for pid in $pids; do wait "$pid"; done
fi
# Host mutates an existing shared file after the guest has already read it.
echo ERBIUM-SHARE-READY
n=0
while [ "$(cat /mnt/host/live.txt)" != 'updated on host' ]; do
    n=$((n + 1)); [ "$n" -lt 20 ] || exit 1
    sleep 1
done
echo "ALL SHARE TESTS PASSED ($mode)"
