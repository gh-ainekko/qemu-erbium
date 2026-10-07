#!/usr/bin/env bash
# Hermetic binary echo/reload test; --kotama tests an optionally embedded real ELF.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
case "$#:${1:-}" in
  0:) exec "$R/scripts/run-uart.sh" --test ;;
  1:--kotama) exec "$R/scripts/run-uart.sh" --kotama-test ;;
  *) echo "usage: $0 [--kotama]" >&2; exit 2 ;;
esac
