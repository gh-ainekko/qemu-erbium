# Step #3: browser IDE ↔ Erbium sysemu

This checkpoint uses the **actual MicroBlocks web IDE**, not a remote desktop.
The GP interpreter, editor, compiler and decompiler run as WebAssembly/GP in
the browser. A small JavaScript serial adapter sends the existing binary
MicroBlocks protocol through a same-origin WebSocket to sysemu's UART.
The application executes on the real RV64 SmallVM firmware in `erbium_emu`.

The private live endpoint is `https://erbium-qemu.exe.xyz/`.
Service installation, browser build provenance and controls are described in
`web/README.md`. The former native/noVNC experiment remains available only as
an optional, separate regression tool.

## Completed validation

The frozen-source browser run **PASS** is
`build/smallvm/web-tests/final-pass/result.json` (Chromium, 50.432 seconds).
It verifies source/asset immutability and matching build provenance, not just
successful page loading.

| Check | Result |
| --- | --- |
| Actual browser compiler output | 22,586 bytes, 27 chunks, largest 937 bytes |
| Board CRCs and readback | All 27 chunks match byte-for-byte |
| Unchanged reconnect | Same emulator, zero redundant chunk uploads |
| Function edit | Exactly one changed chunk; real board result 41 → 42 |
| Decompile/recompile/run | 26 recovered chunks; result still 42 |
| GUI and project I/O | Real file import, Start/Stop, reconnect and Save/download |
| Parser/runtime/network checks | No hidden GP errors or missing assets; no VNC/iframe |
| Wire pacing | Erbium delay 1 on both connections |

The browser driver executes its serial assertions on the main GP API stack
so a second task cannot consume the GUI's replies. After expensive decompiler
layout it services the normal heartbeat before synchronization and requires
26 recovered chunks; an empty CRC dictionary cannot count as success.

Backend validation passed **14 unit tests**, real v416 UART handshake,
reset/crash/shutdown/fatal-I/O recovery, and a 262,144-byte real-MMIO echo.
The client adapter's **4 unit tests** passed too. The existing
`scripts/test-smallvm.sh --with-legacy32` full regression passed.

Live-service smoke evidence is in `build/smallvm/web-live/`: the visible
**Load example → Connect → Start → Variables/answer** path returned **41**,
verified again in raw UART replies. **Reset board** changed the emulator PID,
reconnected and ran the example again. The test browser then disconnected,
leaving the UART available to the user. `smallvm-web.service` is enabled;
the old native/noVNC unit is inactive and disabled.

## Reproduce and inspect

```sh
# Build the pinned browser application after installing its documented tools:
scripts/fetch-smallvm-web.sh

# Real browser tests; Playwright setup is in tests/web_ide.md:
SMALLVM_WEB_TEST_PYTHON="$PWD/build/web-test-venv/bin/python" \
  scripts/test-smallvm-web.sh

# Backend unit and real-emulator protocol tests (isolated ports):
/usr/bin/python3 smallvm/web/test_server.py
/usr/bin/python3 smallvm/web/test_integration.py

# Existing LP64, sanitizer, legacy32 and RV64 firmware regressions:
scripts/test-smallvm.sh --with-legacy32
```

The browser test starts its own backend/emulator on port 8002 and refuses
port 8000. It records screenshots, browser logs/network requests, raw UART
captures, saved/decompiled projects, compiled-chunk CRCs, and source/binary
hashes under `build/smallvm/web-tests/`. A `result.json` with `status: PASS`
is required; helper unit tests or a rendered editor alone are not sufficient.
See `tests/web_ide.md` for the exact assertions and test instrumentation.

The backend tests cover exact duplex transfer with both 1 MiB queue bounds,
partial writes/EAGAIN, binary-only WebSocket frames, one-client exclusivity,
origin/CSRF rejection, proxy-origin handling, reset, emulator crash recovery,
graceful active-client shutdown, and fatal I/O cleanup. An optional echo ELF
adds an all-byte-values test through real emulated UART MMIO.

The actual patched upstream source tree is pinned to
`73844f74e19e41eb466ea0c727a85594902be4c1`
(base `49f337529294640def7b27a2ee1c4e7ba88ccdbb`, SmallVM patches 01–06).
`build/smallvm/web/provenance.json` records the build command and overlay
hashes; `SHA256SUMS` covers the generated assets. Emscripten 3.1.6 uses
Clang/LLVM/LLD 14 explicitly: Clang 15's different `main` symbol handling can
otherwise produce a tiny, nonfunctional WASM artifact.

## Boundaries

- **Program storage is volatile.** Reset restarts the emulator/firmware;
  reconnecting an open project can re-upload it. That is not MRAM persistence.
- Only one browser owns the UART at a time. Disconnect or close its tab before
  using a second one; a second client must not silently steal the connection.
- Browser Save downloads a source project to the user's computer. Preserve it
  before closing/reloading the tab. Backend logs are not project backups.
- Functions, variables, lists, arithmetic, timers and start/stop need no
  physical peripherals. This does not qualify GPIO, physical baud rates,
  flashing physical boards, multicore execution or a finished board package.
- Decompilation recovers executable behavior, not exact original author,
  description, comments or layout. Unsupported source constructs retain the
  upstream decompiler's limitations.
- Authentication is the VM's **private exe.dev proxy**; application origin
  checks are defense in depth, not a substitute for authentication. Do not
  make this development IDE public.
- Durable MRAM storage (#4) and product integration (#5) remain separate work.
