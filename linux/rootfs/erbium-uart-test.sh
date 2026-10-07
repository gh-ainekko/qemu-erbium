#!/bin/sh
# Only the hermetic UART fixture: actual Kotama acceptance is separately opt-in.
# /init owns the ERBIUM-UART-TEST-RESULT marker and shutdown policy.
exec /usr/bin/erbium-uart-test \
    --tty "${UART_TEST_TTY:-/dev/ttyAMA1}" \
    --elf "${UART_TEST_ELF:-/firmware/uart-smoke.elf}" \
    --device "${UART_TEST_DEVICE:-/dev/erbium0}" \
    --mtd "${UART_TEST_MTD:-/dev/mtd0}" \
    --timeout-ms "${UART_TEST_TIMEOUT_MS:-30000}" "$@"
