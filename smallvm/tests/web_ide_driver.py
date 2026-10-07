# SPDX-License-Identifier: Apache-2.0
"""Adapt the native integration assertions, not the application/compiler.

The returned source defines test stages in the already-started browser IDE.
It never overrides startup or any application method. Browser file import/save
are exercised by Playwright separately; reports travel over browserPostMessage.
"""
from pathlib import Path
import re


def replace_once(source, old, new):
    assert source.count(old) == 1, f"native driver changed: {old[:100]!r}"
    return source.replace(old, new)


def assert_clean_browser_gp_log(lines):
    from ide_integration import assert_clean_gp_log
    # gp.c's Emscripten branch prints this banner before browserStep, even
    # with the real GUI startup. Unlike native GP, this is not a fallback REPL.
    clean = [line.split(": ", 1)[-1] for line in lines]
    welcome = [i for i, line in enumerate(clean) if line == "Welcome to GP!"]
    loaded = [i for i, line in enumerate(clean)
              if re.fullmatch(r"Loaded \d+ library files from embedded file system", line)]
    assert len(welcome) == len(loaded) == 1 and loaded[0] < welcome[0], (
        "missing/duplicate browser GP startup banners")
    del clean[welcome[0]]
    assert_clean_gp_log("\n".join(clean))


def browser_driver():
    source = Path(__file__).with_name("ide_integration.gp").read_text()
    helpers = source[:source.index("to ideAcceptSaveDialog")]
    # Connection and user controls are real browser clicks between main-API
    # stages. Never race a child GP task against the editor's UART consumer.
    connect_begin = helpers.index("to ideConnect ")
    connect_end = helpers.index("to ideGetVar ", connect_begin)
    helpers = helpers[:connect_begin] + helpers[connect_end:]
    helpers = replace_once(helpers,
        "writeFile (join (at (global 'ideTestConfig') 'output') '/gp-result.json') (jsonStringify result)",
        "browserPostMessage (array 'web-ide-result' (jsonStringify result))")
    helpers = replace_once(helpers, "    exit\n", "    error label\n")
    helpers = replace_once(helpers,
        "writeFile (join (at (global 'ideTestConfig') 'output') '/readback.json') (jsonStringify chunks)",
        "browserPostMessage (array 'web-ide-readback' (jsonStringify chunks))")
    helpers = replace_once(helpers,
        "  expected = (getField rt 'chunkIDs')",
        """  expected = (getField rt 'chunkIDs')
  ideAssert ((count expected) > 0) 'CRC proof cannot accept zero compiler chunks'""")
    run = source[source.index("to startup {"):]
    run = run[run.index("  scripts = (sortedScripts"):]
    assert run.count("  ideConnect rt (at config 'port')\n") == 2
    run = run.replace("  ideConnect rt (at config 'port')\n",
                      "  ideAssert (connectedToBoard rt) 'browser GUI connected real firmware'\n", 1)
    save_begin = run.index("  savePath = ")
    save_end = run.index("  closePort rt", save_begin)
    run = run[:save_begin] + """  ideAssert (saveLoadTest (project (scripter rt))) 'IDE project save/load roundtrip'
  browserPostMessage (array 'web-ide-original-source' (codeString (project (scripter rt))))
""" + run[save_end:]
    run = replace_once(run, "  closePort rt\n  ideConnect rt (at config 'port')\n", "")
    run = replace_once(run,
        "  donor = (loadFromString (newMicroBlocksProject) (readFile (at config 'edited')) false)",
        "  donor = (loadFromString (newMicroBlocksProject) (at config 'edited') false)")
    run = replace_once(run,
        "  writeFile (join (at config 'output') '/decompiled.ubp') recoveredSource",
        "  browserPostMessage (array 'web-ide-decompiled-source' recoveredSource)")
    run = replace_once(run, "  installDecompiledProject rt recovered\n", """  installDecompiledProject rt recovered
  // Restoring this large browser GUI can exceed the normal 8-second heartbeat
  // age while the main API owns the GP stack. Reestablish actual board liveness
  // and do a normal unchanged/incremental sync, just as GUI stepping does after
  // returning. No serial-delay, version or parser state is faked.
  waitForPing rt
  ideAssert (connectedToBoard rt) 'firmware responds after large recovered GUI restore'
  stopAndSyncScripts rt
  expectedRecovered = (+ (count (allFunctions recovered)) (count (scripts (main recovered))))
  ideAssert ((count (getField rt 'chunkIDs')) == expectedRecovered) 'all recovered source chunks are compiled/uploaded'
""")
    for expected, field, label in (
        (41, "observed_answer_initial", "function result is 41"),
        (42, "observed_answer_edited", "incrementally compiled function result is 42"),
        (42, "observed_answer_recovered", "decompile/recompile/upload roundtrip executes correctly"),
    ):
        run = replace_once(run,
            f"  ideAssert ((ideGetVar rt 'answer') == {expected}) '{label}'",
            f"""  answerValue = (ideGetVar rt 'answer')
  atPut result '{field}' answerValue
  ideAssert (answerValue == {expected}) '{label}'""")
    for field, label in (
        ("observed_status_initial", "string variable is running"),
        ("observed_status_recovered", "roundtrip retains string variable"),
    ):
        run = replace_once(run,
            f"  ideAssert ((ideGetVar rt 'status') == 'running') '{label}'",
            f"""  statusValue = (ideGetVar rt 'status')
  atPut result '{field}' statusValue
  ideAssert (statusValue == 'running') '{label}'""")
    for line in (
        "  atPut result 'shared_native_compat' true\n",
        "  atPut result 'native_save_dialog_exercised' true\n",
        "  atPut result 'native_save_initial_directory' (global 'ideSaveInitialDirectory')\n",
    ):
        run = replace_once(run, line, "")
    run = replace_once(run,
        "  writeFile (join (at config 'output') '/gp-result.json') (jsonStringify result)",
        "  browserPostMessage (array 'web-ide-result' (jsonStringify result))")
    run = replace_once(run, "  exit\n", "")
    assert run.endswith("}\n")
    run = run[:-2]
    # Preserve observations from Prepare across the later, separate stage.
    final = run.index("  atPut result 'status' 'PASS'")
    finish = run[final:].replace("'compiled_bytes' total", "'compiled_bytes' (at result 'compiled_bytes')").replace(
        "'largest_chunk_bytes' largest", "'largest_chunk_bytes' (at result 'largest_chunk_bytes')")
    for field, variable in (("observed_counter_start", "counter1"),
                            ("observed_counter_end", "counter2"),
                            ("observed_counter_stopped", "stopped2"),
                            ("observed_elapsed_milliseconds", "elapsed"),
                            ("observed_history", "history")):
        finish = replace_once(finish, f"  atPut result '{field}' {variable}\n", "")
    run = run[:final] + finish
    start = run.index("  startAll rt\n")
    stop = run.index("  sendStopAll rt\n", start)
    reconnect = run.index("  reconnected = (ideCRCs rt)", stop)
    common = """  config = (global 'ideTestConfig')
  result = (global 'ideTestResult')
  editor = (findMicroBlocksEditor)
  rt = (smallRuntime)
"""
    prepare = """to webIdePrepare {
  result = (dictionary)
  setGlobal 'ideTestResult' result
  editor = (findMicroBlocksEditor)
  ideAssert (notNil editor) 'real browser GUI editor exists'
  rt = (smallRuntime)
""" + run[:start] + """  setGlobal 'webIdeCRCs' crcs
  return true
}
"""
    running = "to webIdeObserveRunning {\n" + common + run[start + len("  startAll rt\n"):stop] + "  return true\n}\n"
    stopped = "to webIdeObserveStopped {\n" + common + run[stop + len("  sendStopAll rt\n"):reconnect] + "  return true\n}\n"
    roundtrip = "to webIdeRoundtrip {\n" + common + "  crcs = (global 'webIdeCRCs')\n" + run[reconnect:] + "  return true\n}\n"
    return helpers + "\n" + prepare + running + stopped + roundtrip


def verify_wire(outgoing_data, incoming_data, result, readback_artifact=None):
    """Independently inspect real server UART bytes, not GP's claimed success."""
    from ide_integration import frames
    outgoing, tail = frames(outgoing_data)
    incoming, incoming_tail = frames(incoming_data)
    assert tail == incoming_tail == 0, "incomplete captured UART frames"
    delays = [body[0] for op, ident, body in outgoing if op == 30 and ident == 1 and body]
    assert len(delays) >= 2 and all(value == 1 for value in delays), delays
    downloads = [body for op, _, body in outgoing if op == 32]
    expected = (result["chunk_count"] + result["incrementally_changed_chunks"]
                + result["recompiled_chunk_count"])
    assert len(downloads) == expected, f"redundant/missing uploads: {len(downloads)} != {expected}"
    assert sum(map(len, downloads)) > 16384, "missing >16KB compiler payload"
    assert max(map(len, downloads)) >= 900, "missing near-1KB chunk"
    assert any(op == 21 for op, _, _ in incoming), "no real variable replies"
    assert not any(op == 19 for op, _, _ in incoming), "firmware task error"
    readbacks = [(ident, body) for op, ident, body in incoming if op == 32]
    assert len(readbacks) == result["chunk_count"], "missing readback chunks"
    assert len(set(ident for ident, _ in readbacks)) == len(readbacks), "duplicate readback chunks"
    # Compare *captured* readback to the latest real uploads before the request.
    # GP upload frames have a final 254 transport terminator; replies do not.
    uploaded = {}
    requests = 0
    for opcode, ident, body in outgoing:
        if opcode == 13:
            requests += 1
            break
        if opcode == 32:
            assert body[-1] == 254, "upload terminator missing"
            uploaded[ident] = body[:-1]
    assert requests == 1, "no actual getAllCode request"
    assert uploaded == dict(readbacks), "captured bytecode upload/readback differs"
    if readback_artifact is not None:
        reported = {ident: bytes([kind, *code]) for ident, kind, code in readback_artifact}
        assert len(reported) == len(readback_artifact), "duplicate artifact chunk"
        assert reported == dict(readbacks), "GP artifact does not match raw UART readback"
    return dict(downloaded_chunk_frames=len(downloads),
                downloaded_chunk_bytes=sum(map(len, downloads)),
                readback_chunk_frames=len(readbacks), redundant_chunk_downloads=0,
                wire_readback_bytes_exact=True,
                observed_serial_delay_requests=delays,
                incoming_frames=len(incoming), outgoing_frames=len(outgoing))
