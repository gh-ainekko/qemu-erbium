# Real native MicroBlocks integration

Run from the repository root after building the SmallVM firmware and emulator:

```sh
python3 smallvm/tests/ide_integration_test.py
python3 smallvm/tests/ide_integration.py
# Deliberately split serial traffic into seven-byte relay writes:
python3 smallvm/tests/ide_integration.py \
  --relay-write-size 7 --output build/smallvm/ide-tests-fragmented
```

Dependencies: Python 3, native GP at `ext/smallvm/gp/gp-linux64bit`, Xvfb and
`xauth` (`xvfb-run`). There is no package installation, service setup, TCP
listener, or use of the live IDE display/serial port. Every run owns two raw
PTYs, its emulator, and an automatically chosen X display. The Python wrapper
terminates owned process groups on success, failure, or the overall timeout.
Exit status is zero only after **both** GP assertions and independent UART
transcript checks pass. `--emu`, `--gp`, `--native-compat`, `--timeout`, `--output`,
and an optional firmware ELF positional argument select alternate artifacts.

## What is actually tested

The native GP process runs from its `gp` directory, loads all
`runtime/lib/*.gp`, `loadIDE.gp`, the same `smallvm/ide/native-compat.gp` UI
compatibility layer used by the live IDE, and finally `ide_integration.gp` to
replace **startup only**. The runtime methods under test are the upstream
methods with the repository's selected-port preservation and Erbium default
serial-pacing patches applied. The live `launch.gp` is deliberately **not**
loaded: its serial-path selection is unnecessary and tests must not attach to
the live UI's service alias.
No compiler, serial runtime, decompiler, or project loader method is replaced
or mocked by the harness. The driver creates the real MicroBlocks editor/page
without entering the infinite interactive event loop. Browser-shell
compatibility and the native project save implementation come from the
shared application compatibility layer, not test-only replacements.

* `MicroBlocksEditor.openProject` loads a generated `.ubp` source project.
* `SmallRuntime.compiledBytesFor` proves there is a near-1KB chunk and more
  than 16KB total compiler output, without the oversized-script error stub.
* The real serial connect/version path and `stopAndSyncScripts` upload all
  code; bulk board CRC replies match every actual compiled chunk.
* `startAll`, real `getVar` replies, and the normal runtime message parser
  verify function result 41, string status, indexed list, a timer-paced
  counter that advances, and a counter that freezes after stopping.
* The live application's native `saveProject` opens the actual modal
  `MicroBlocksFilePicker`; a scheduled GP helper navigates its real folder
  method and invokes its acceptance callback. No picker/write primitive is
  mocked. The native save method writes the file and the IDE's own
  `saveLoadTest` checks it. `save-dialog.png` captures the real native dialog.
* Close/reopen of the same PTY takes the IDE reconnect path and preserves
  all board CRCs.
* An edit to the project's real Function AST is marked for recompilation
  and synchronized normally. Exactly one CRC changes and the result becomes
  42. This is a programmatic source-model edit, not simulated mouse dragging.
* Actual `getVarNamesMsg`/`getAllCodeMsg` UART responses populate the real
  `MicroBlocksDecompiler` through `SmallRuntime.receivedChunk`. All returned
  code bytes exactly match the compiler output, including word padding.
* The actual decompiler produces a project, which is installed with
  `installDecompiledProject`, recompiled, uploaded, CRC-checked, and run.
  The result is still 42 and the status string survives.
* Python independently requires complete captured frames, no task errors,
  actual board variable replies, near-1KB chunk frames, and >16KB uploaded
  compiled chunk payloads. It rejects duplicate/missing readback chunks and
  redundant uploads: exactly initial chunks + one edited chunk + recovered
  chunks may be downloaded, so reconnect and unchanged sync cannot quietly
  retransmit the project.
* Parser/undefined-function errors, debugger stack traces, missing assets,
  serial errors, and
  fallback GP REPL messages in `gp.log` are hard failures even if a later
  assertion/result reports PASS. Native icons and shell hooks are supplied
  by the same compatibility file as the live IDE.
* Captured connect/reconnect default serial-profile requests must all select
  delay 1. This catches the initialization-order regression where default
  pacing was selected before the incoming version identified Erbium.

The showcase `smallvm/examples/Erbium Showcase.ubp` uses only variables,
lists, arithmetic, a custom function, and timers: **no GPIO**. The generated
test project adds 24 unique long literals in separate source scripts.
This is an aggregate >16KB multi-chunk upload through the IDE's real paced
63-byte serial writes, **not** a single unpaced >16KB write. The showcase
waits 100ms; the generated test waits 5ms because simulated time need not
track wall time. Counter progression is polled with a **20-second wall-time
deadline**, not assumed from one fixed sleep; observed counter values and
elapsed simulated milliseconds are recorded. Readback uses the real IDE's
Erbium default serial-delay setting without a test-specific delay override.

## Artifacts and intentionally non-identical roundtrip

Default directory: `build/smallvm/ide-tests/`.

* `result.json`: final status, assertions' summary, byte/frame counts,
  elapsed time, launch commands, and artifact SHA-256s.
* `gp-result.json`: GP assertions' result; never accepted if missing/stale.
* `gp.log`, `emulator.log`: separated diagnostics.
* `ide-to-board.bin`, `board-to-ide.bin`: complete binary wire capture.
* `large.ubp`, `edited.ubp`: deterministic source inputs.
* `saved.ubp`: actual IDE save output.
* `save-dialog.png`: actual native save dialog after folder navigation.
* `readback.json`: exact chunks returned by the real firmware.
* `decompiled.ubp`: real decompiler source output.

Bytecode readback is exact. **Source text is not identical:** the upstream
decompiler assigns new/random script coordinates, drops the standalone
comment, loses project author/description metadata and function input-default
metadata, and may normalize source formatting/ordering and variable ordering.
The harness records
`decompiled_source_exact: false`, verifies exactly one script (the comment)
was dropped, records original/recovered author and description, and tests
save/load plus runtime semantics of the recovered
source. It does not assert that the recompiled CRCs equal the original CRCs:
chunk IDs, variable indices, and metadata can legitimately change.
The upstream native picker initially chooses `Downloads` even if given an
absolute filename. The harness records `native_save_initial_directory` and
navigates the real picker to the isolated output folder before acceptance.

The helper unit tests only cover framing/generation. They do not count as
IDE interoperability. No physical UART timing, real board peripherals,
browser/mouse UI automation, or malformed-input firmware qualification is
claimed. Native save-dialog behavior **is** exercised through real callbacks.
