# Real browser IDE regression

This is a **web application**, not noVNC or a remote desktop. Chromium loads the
self-hosted upstream GP interpreter, runtime libraries, MicroBlocks compiler,
decompiler and GUI. Its actual serial host functions carry raw binary UART over
WebSocket to the real Erbium sysemu process running the real SmallVM firmware.
Neither compiler nor serial is mocked.

## Run

Build dependencies are documented by `scripts/fetch-smallvm-web.sh`. Browser
automation is separate from the backend's distro Python/aiohttp:

```sh
python3 -m venv build/web-test-venv
build/web-test-venv/bin/pip install -r smallvm/tests/web_ide_requirements.txt
build/web-test-venv/bin/python -m playwright install chromium
SMALLVM_WEB_TEST_PYTHON="$PWD/build/web-test-venv/bin/python" \
  scripts/test-smallvm-web.sh
```

If `uv` is installed, `uv venv build/web-test-venv` and
`uv pip install --python build/web-test-venv/bin/python -r
smallvm/tests/web_ide_requirements.txt` are equivalent dependency setup.
No test installs packages automatically.

For already-built artifacts:

```sh
SMALLVM_WEB_TEST_SKIP_BUILD=1 \
SMALLVM_WEB_TEST_PYTHON="$PWD/build/web-test-venv/bin/python" \
SMALLVM_WEB_TEST_BUILD="$PWD/build/smallvm/web-tests/my-fresh-run" \
  scripts/test-smallvm-web.sh
```

Each run needs a **fresh output directory**. The default is timestamped. Port
8002 is the default; `SMALLVM_WEB_TEST_PORT` chooses another isolated loopback
port. Port **8000 is explicitly refused**. An occupied port fails binding
preflight rather than accidentally testing an existing/live server. The test
owns and terminates its server/emulator process group. It does not use the live
service, desktop display or serial alias. The backend interpreter defaults to
`/usr/bin/python3`; use `--server-python` for an alternate aiohttp environment.
Other CLI options include `--elf`, `--emu`, `--assets`, `--timeout` and `--headed`
(the latter needs an actual/Xvfb display).

The helper tests, `python3 smallvm/tests/web_ide_test.py`, test adaptation/framing
only. They **do not** count as browser/firmware interoperability.

## Assertions and coverage

* Actual GP GUI startup: visible, nonblank canvas of useful dimensions,
  populated category list, ready event and enabled Connect button.
* No iframe, no noVNC/websockify requests. The only observed WebSockets must be
  the isolated application's `/uart` connections.
* The original `ide_integration.py` generator produces the same deterministic
  large source project and one-function edited source. Real DOM file input,
  upstream FileReader/drop route and `MicroBlocksEditor.openProject` import it.
* Real Project menu **Save** downloads the actual recovered project source,
  verified against `codeString`. This is browser download behavior, not a native
  picker.
* Real **Connect**, **Start** and **Stop** controls invoke their actual callbacks.
  Compiler output has a near-1KB chunk and exceeds 16KB total. The board bulk
  CRC replies match every real compiled chunk.
* Actual firmware variable replies check custom-function result 41, string
  `running`, initialized/indexed list, positive elapsed timer, an advancing
  timer-paced counter, and a counter frozen after stopping.
  The normal browser Stop callback also clears variables; the stopped counter
  can legitimately be zero. The test observes and records this behavior.
* Actual WebSocket close/reconnect through the Connect control preserves all
  CRCs and does not restart the emulator or redundantly upload unchanged code.
* One project Function AST edit (not mouse dragging or hand-built bytecode)
  synchronizes exactly one changed chunk; the board result becomes 42.
* Actual get-variable-names/get-all-code replies populate the real decompiler.
  Every readback byte matches padded compiler output. The recovered source is
  installed, recompiled, reuploaded and run; result 42 and string survive.
  Large browser GUI reconstruction may exceed the normal eight-second heartbeat
  age while a main API stage owns GP. After the real install the driver requests
  an actual ping and performs normal synchronization; it asserts the recovered
  nonzero chunk count. Unchanged code must still cause **no redundant uploads**.
* Python independently parses raw backend UART capture files: complete frames,
  no firmware task errors, real variable replies, exact readback chunk count,
  unique readback IDs, and exactly initial + edited-one + recovered uploads.
  Connect/reconnect serial profile requests must both choose Erbium delay 1.
* Missing assets, failed requests, browser exceptions and GP parser/debugger/
  serial errors are failures even if GP later reports PASS.

`web_ide_driver.py` adapts the existing native driver with checked textual
anchors; a native-driver change cannot silently remove assertions. It defines
test functions in the **already-running** GP module and invokes four stages
through the real main GP API dispatcher. It never overrides startup or
compiler, runtime, decompiler, serial parser or application methods. Reports
and artifacts use the upstream `browserPostMessage` primitive. Actual Playwright
clicks perform reconnect and first start/stop **between** stage calls. GP waits
yield to JavaScript so real WebSocket bytes arrive, without a competing launched
task or editor callback stealing UART responses. No artificial busy flags or
test-only runtime method replacements are used.

Decompiler source is intentionally **not text-identical**: the upstream
decompiler changes coordinates, omits the standalone comment and drops author/
description/input-default metadata. Bytecode readback is exact; recovered
source correctness is established by save/load and real execution, not a false
claim of identical recompiled CRCs.

No GPIO, physical UART timing, malformed firmware inputs, persistent storage,
VM firmware-flashing UI or drag-and-drop block editing is claimed. The firmware
ELF is loaded by sysemu; "upload" here means compiled MicroBlocks program upload
through the actual IDE UART protocol.

## Evidence

`result.json` records final status, any failure, semantic observations, UART
counts, compiler/chunk sizes, browser version, backend launch command and
emulator identity, SHA-256s of ELF/emulator/browser assets/test+application+
firmware sources, pinned SmallVM tree, and browser build provenance. A GP PASS
alone is insufficient: independent Python checks must also pass.

Other files include `gp-result.json`, `browser.log`, `browser-network.json`,
`trace.zip` (Playwright viewer), GUI screenshots, deterministic `large.ubp` and
`edited.ubp`, real `imported.ubp`/`saved.ubp`, `original.ubp`, `decompiled.ubp`,
exact `readback.json`, and generated `browser-driver.gp`. The `server/` directory
contains raw UART captures, emulator stdout/stderr and structured lifecycle
events. Failed runs preserve logs/provenance too and attempt a failure screenshot,
state dump and trace. No stale result is accepted.

Open a trace with:

```sh
build/web-test-venv/bin/python -m playwright show-trace \
  build/smallvm/web-tests/my-fresh-run/trace.zip
```

## Recorded validation

The frozen frontend build at commit `7967457` passed the complete test on
October 7, 2026: `build/smallvm/web-tests/final-pass/result.json`.
Chromium `153.0.8010.12` completed in 50.432 seconds. The real compiler produced
22,586 bytes, largest chunk 937 bytes, with 27 initial and 26 recovered chunks.
The wire contained exactly 54 uploads (45,616 payload bytes), 27 byte-identical
readback chunks, zero redundant uploads and delay requests `[1, 1]`.
The counter advanced 4 → 7, elapsed simulated time was 35ms, and the real Stop
callback cleared/froze the counter at zero. Function results 41 → 42, list/string
values, GUI startup/start/stop/reconnect, browser Save download and clean logs
all passed. Source, loaded assets and firmware/emulator binaries remained
unchanged during the run. Exact source/binary SHA-256s are in the JSON result.
