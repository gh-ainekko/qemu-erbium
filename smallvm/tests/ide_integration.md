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
transcript checks pass. `--emu`, `--gp`, `--native-glue`, `--timeout`, `--output`,
and an optional firmware ELF positional argument select alternate artifacts.

## What is actually tested

The native GP process runs from its `gp` directory, loads all
`runtime/lib/*.gp`, `loadIDE.gp`, and finally `ide_integration.gp` to replace
**startup only**. By default the runtime methods under test are the upstream
methods with the repository's selected-port preservation patch applied.
`--native-glue FILE` can additionally load shared native launch glue before
the test driver; the default deliberately avoids overriding runtime methods.
No compiler, runtime, decompiler, project loader, or project save method is
mocked. The driver creates the real MicroBlocks editor/page without entering
the infinite interactive event loop. Missing native browser-shell primitives
(HTML property notifications, dropped browser files, WebSerial capability)
are explicit no-ops; the browser download primitive writes the output file.
Those are not serial, compiler, or board-response substitutes.

* `MicroBlocksEditor.openProject` loads a generated `.ubp` source project.
* `SmallRuntime.compiledBytesFor` proves there is a near-1KB chunk and more
  than 16KB total compiler output, without the oversized-script error stub.
* The real serial connect/version path and `stopAndSyncScripts` upload all
  code; bulk board CRC replies match every actual compiled chunk.
* `startAll`, real `getVar` replies, and the normal runtime message parser
  verify function result 41, string status, indexed list, a timer-paced
  counter that advances, and a counter that freezes after stopping.
* The unmodified IDE `saveProject` writes current project source through
  the file-download boundary; the IDE's own `saveLoadTest` checks it.
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

The showcase `smallvm/examples/Erbium Showcase.ubp` uses only variables,
lists, arithmetic, a custom function, and timers: **no GPIO**. The generated
test project adds 24 unique long literals in separate source scripts.
This is an aggregate >16KB multi-chunk upload through the IDE's real paced
63-byte serial writes, **not** a single unpaced >16KB write. The showcase
waits 100ms; the generated test waits 5ms because simulated time need not
track wall time. Readback uses the IDE's supported minimum serial-delay
setting, avoiding long physical-board per-word sleeps in slow simulation.

## Artifacts and intentionally non-identical roundtrip

Default directory: `build/smallvm/ide-tests/`.

* `result.json`: final status, assertions' summary, byte/frame counts,
  elapsed time, launch commands, and artifact SHA-256s.
* `gp-result.json`: GP assertions' result; never accepted if missing/stale.
* `gp.log`, `emulator.log`: separated diagnostics.
* `ide-to-board.bin`, `board-to-ide.bin`: complete binary wire capture.
* `large.ubp`, `edited.ubp`: deterministic source inputs.
* `saved.ubp`: actual IDE save output.
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

The helper unit tests only cover framing/generation. They do not count as
IDE interoperability. No physical UART timing, real board peripherals,
browser UI automation, or malformed-input firmware qualification is claimed.
