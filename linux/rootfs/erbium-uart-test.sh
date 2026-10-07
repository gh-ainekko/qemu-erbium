#!/bin/sh
# Hermetic fixture by default; --kotama opts into /firmware/host-payload.elf.
# /init owns ERBIUM-UART-TEST-RESULT / ERBIUM-KOTAMA-UART-TEST-RESULT and shutdown.
# Preserve the C client's mode-dependent default unless explicitly overridden.
[ -z "${UART_TEST_ELF:-}" ] || set -- --elf "$UART_TEST_ELF" "$@"
exec /usr/bin/erbium-uart-test \
    --tty "${UART_TEST_TTY:-/dev/ttyAMA1}" \
    --device "${UART_TEST_DEVICE:-/dev/erbium0}" \
    --mtd "${UART_TEST_MTD:-/dev/mtd0}" \
    --timeout-ms "${UART_TEST_TIMEOUT_MS:-30000}" "$@"
