#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Isolated real-browser regression; never connect to the live IDE on port 8000.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
PY=${SMALLVM_WEB_TEST_PYTHON:-python3}
OUT=${SMALLVM_WEB_TEST_BUILD:-"$R/build/smallvm/web-tests/$(date +%Y%m%d-%H%M%S)"}
PORT=${SMALLVM_WEB_TEST_PORT:-8002}
[[ "$PORT" != 8000 ]] || { echo 'Refusing live port 8000' >&2; exit 1; }
"$PY" -c 'import playwright.sync_api' || {
  echo 'Install Playwright in a venv, run python -m playwright install chromium, and set SMALLVM_WEB_TEST_PYTHON.' >&2
  exit 1
}
mkdir -p "$OUT"
if [[ ${SMALLVM_WEB_TEST_SKIP_BUILD:-0} != 1 ]]; then
  make -C "$R/smallvm" firmware > "$OUT/firmware-build.log" 2>&1
  "$R/scripts/fetch-smallvm-web.sh" > "$OUT/browser-build.log" 2>&1
fi
"$PY" "$R/smallvm/tests/web_ide_test.py" 2>&1 | tee "$OUT/helper-tests.log"
"$PY" "$R/smallvm/tests/web_ide_integration.py" \
  --port "$PORT" --output "$OUT" \
  --assets "${SMALLVM_WEB_BUILD:-$R/build/smallvm/web}" \
  --emu "${ERBIUM_EMU:-$R/dist/bin/erbium_emu}" \
  "$@" > "$OUT/summary.log" 2>&1 || {
    cat "$OUT/summary.log" >&2
    exit 1
  }
echo "PASS real browser GP/compiler/sysemu integration; evidence: $OUT"
