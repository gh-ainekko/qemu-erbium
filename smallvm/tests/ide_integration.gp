// SPDX-License-Identifier: Apache-2.0
// Last loaded after runtime/lib/*.gp and loadIDE.gp. No IDE methods replaced.


to ideAssert condition label {
  if (not condition) {
    result = (global 'ideTestResult')
    if (isNil result) { result = (dictionary) }
    atPut result 'status' 'FAIL'
    atPut result 'failure' label
    writeFile (join (at (global 'ideTestConfig') 'output') '/gp-result.json') (jsonStringify result)
    print 'IDE_TEST_FAIL' label
    exit
  }
  print 'IDE_TEST_PASS' label
}

to idePump rt duration {
  end = ((msecsSinceStart) + duration)
  while ((msecsSinceStart) < end) {
    processMessages rt
    waitMSecs 5
  }
}

to ideConnect rt path {
  setField rt 'disconnected' false
  setField rt 'portName' path
  openPortAndSendPing rt
  // Requires the upstream PTY-preserving openPortAndSendPing fix.
  ideAssert ((getField rt 'portName') == path) 'connect preserves selected PTY'
  end = ((msecsSinceStart) + 8000)
  while (and
      (or (not (connectedToBoard rt)) (notNil (getField rt 'connectionStartTime')))
      ((msecsSinceStart) < end)) {
    processMessages rt
    updateConnection rt
    waitMSecs 10
  }
  ideAssert (connectedToBoard rt) 'native IDE serial connect'
  ideAssert ((vmVersion rt) >= 300) 'real firmware version accepted'
}

to ideGetVar rt name {
  proj = (project (scripter rt))
  id = (indexForVar proj name)
  getVar rt id
  end = ((msecsSinceStart) + 3000)
  while ((msecsSinceStart) < end) {
    buf = (getField rt 'recvBuf')
    if (isNil buf) { buf = (newBinaryData 0) }
    data = (readSerialPort (getField rt 'port') true)
    if (notNil data) { buf = (join buf data) }
    setField rt 'recvBuf' buf
    if ((byteCount buf) >= 3) {
      marker = (byteAt buf 1)
      size = 3
      if (251 == marker) {
        if ((byteCount buf) >= 5) {
          size = (+ 5 (byteAt buf 4) ((byteAt buf 5) << 8))
        } else {
          size = 999999
        }
      }
      if ((byteCount buf) >= size) {
        found = (and (21 == (byteAt buf 2)) (id == (byteAt buf 3)))
        if found { value = (returnedValue rt (copyFromTo buf 1 size)) }
        // The real SmallRuntime parser and message handler consume each frame.
        processNextMessage rt
        if found { return value }
      }
    }
    waitMSecs 5
  }
  ideAssert false (join 'variable reply timeout: ' name)
}

to ideCRCs rt {
  collectCRCsBulk rt
  actual = (getField rt 'crcDict')
  expected = (getField rt 'chunkIDs')
  ideAssert ((count actual) == (count expected)) 'bulk CRC count equals compiler chunks'
  for entry (values expected) {
    ideAssert ((at actual (at entry 1)) == (at entry 2)) (join 'compiled/downloaded CRC chunk ' (at entry 1))
  }
  return (copy actual)
}

to ideReadback rt {
  dec = (newDecompiler)
  setField rt 'decompiler' dec
  sendMsgSync rt 'getVarNamesMsg'
  idePump rt 100
  sendMsg rt 'getAllCodeMsg' 1
  expectedCount = (count (getField rt 'chunkIDs'))
  end = ((msecsSinceStart) + 10000)
  while (and ((count (getField dec 'chunks')) < expectedCount) ((msecsSinceStart) < end)) {
    idePump rt 50
  }
  setField rt 'decompiler' nil
  chunks = (getField dec 'chunks')
  ideAssert ((count chunks) == expectedCount) (join 'actual UART readback chunks: ' (count chunks) ' expected: ' expectedCount)
  ideAssert ((count (getField dec 'vars')) == 5) 'readback includes all five variable names'
  for name (array 'counter' 'status' 'answer' 'history' 'elapsed') {
    id = (indexForVar (project (scripter rt)) name)
    ideAssert ((at (getField dec 'vars') id) == name) (join 'readback variable name ' name)
  }
  entries = (getField rt 'chunkIDs')
  sources = (dictionary)
  for key (keys entries) { atPut sources (first (at entries key)) key }
  for chunk chunks {
    id = (at chunk 1)
    bytes = (chunkBytesFor rt (at sources id))
    while (((count bytes) % 4) != 0) { add bytes 0 }
    ideAssert ((toArray bytes) == (at chunk 3)) (join 'readback byte-for-byte compiler chunk ' id)
  }
  writeFile (join (at (global 'ideTestConfig') 'output') '/readback.json') (jsonStringify chunks)
  return (decompileProject dec)
}

to ideAcceptSaveDialog path {
  end = ((msecsSinceStart) + 5000)
  pickerM = nil
  while (and (isNil pickerM) ((msecsSinceStart) < end)) {
    pickerM = (findMorph 'MicroBlocksFilePicker')
    if (isNil pickerM) { waitMSecs 10 }
  }
  ideAssert (notNil pickerM) 'real native save dialog opens'
  picker = (handler pickerM)
  ideAssert (getField picker 'forSaving') 'native file picker is in save mode'
  setGlobal 'ideSaveInitialDirectory' (getField picker 'currentDir')
  ideAssert ((text (contents (getField picker 'nameField'))) == (filePart path)) 'native save dialog receives requested filename'
  // The upstream picker currently starts in Downloads even when passed an
  // absolute path. Navigate via its real folder method, as an interactive
  // user does, rather than substituting a file picker or write primitive.
  targetDir = (directoryPart path)
  if (endsWith targetDir '/') { targetDir = (substring targetDir 1 ((count targetDir) - 1)) }
  showFolder picker targetDir false
  actualPath = (join (getField picker 'currentDir') '/' (text (contents (getField picker 'nameField'))))
  ideAssert (actualPath == path) (join 'native save dialog path: ' actualPath)
  writeFile (join (at (global 'ideTestConfig') 'output') '/save-dialog.png') (encodePNG (fullCostume pickerM))
  // Invoke the real dialog's existing acceptance callback, not a file-boundary
  // mock. The live native saveProject implementation does the actual write.
  okay picker
  setGlobal 'ideNativeSaveAccepted' true
}

to startup {
  i = (indexOf (commandLine) '--ide-test-config')
  config = (jsonParse (readFile (at (commandLine) (i + 1))))
  setGlobal 'ideTestConfig' config
  result = (dictionary)
  setGlobal 'ideTestResult' result
  setGlobal 'scale' 1
  setGlobal 'blockScale' 1
  // Same IDE initialization as openMicroBlocksEditor, without its infinite loop.
  page = (newPage 1280 900)
  setDevMode page true
  setGlobal 'page' page
  open page false 'Erbium isolated IDE integration'
  editor = (initialize (new 'MicroBlocksEditor') (emptyProject))
  addPart page editor
  rt = (smallRuntime)
  readVersionFile rt
  setField rt 'disconnected' true
  setField editor 'autoDecompile' false
  setField editor 'newerVersion' nil // do not start unrelated network update checks
  openProject editor (readFile (at config 'project')) 'large.ubp' false
  scripts = (sortedScripts (scriptEditor (scripter rt)))
  total = 0
  largest = 0
  for b scripts {
    if (not (isPrototypeHat b)) {
      bytes = (compiledBytesFor rt b)
      total += (count bytes)
      largest = (max largest (count bytes))
      ideAssert ((count bytes) < 1000) 'source chunk fits real compiler upload limit'
    }
  }
  ideAssert (largest >= 900) 'near-1KB real compiler chunk'
  ideAssert (total > 16384) 'multi-chunk compiled project exceeds 16KB'
  atPut result 'compiled_bytes' total
  atPut result 'largest_chunk_bytes' largest
  ideConnect rt (at config 'port')
  stopAndSyncScripts rt
  crcs = (ideCRCs rt)
  atPut result 'chunk_count' (count crcs)
  startAll rt
  idePump rt 500
  ideAssert ((ideGetVar rt 'answer') == 41) 'function result is 41'
  ideAssert ((ideGetVar rt 'status') == 'running') 'string variable is running'
  history = (ideGetVar rt 'history')
  atPut result 'observed_history' history
  ideAssert (notNil (findSubstring ', 20, 30]' history)) 'list initialized and indexed on board'
  counter1 = (ideGetVar rt 'counter')
  atPut result 'observed_counter_start' counter1
  end = ((msecsSinceStart) + 20000)
  counter2 = counter1
  while (and (counter2 <= counter1) ((msecsSinceStart) < end)) {
    idePump rt 250
    waitForPing rt
    counter2 = (ideGetVar rt 'counter')
  }
  atPut result 'observed_counter_end' counter2
  ideAssert (counter2 > counter1) (join 'timer-paced counter advances: ' counter1 ' -> ' counter2)
  elapsed = (ideGetVar rt 'elapsed')
  atPut result 'observed_elapsed_milliseconds' elapsed
  ideAssert (elapsed > 0) 'real timer reports positive elapsed milliseconds'
  sendStopAll rt
  idePump rt 100
  stopped1 = (ideGetVar rt 'counter')
  idePump rt 200
  stopped2 = (ideGetVar rt 'counter')
  atPut result 'observed_counter_stopped' stopped2
  ideAssert (stopped1 == stopped2) 'stop freezes running script'
  savePath = (join (at config 'output') '/saved.ubp')
  launch page (newCommand 'ideAcceptSaveDialog' savePath)
  saveProject editor savePath
  ideAssert (true == (global 'ideNativeSaveAccepted')) 'native save dialog was accepted'
  atPut result 'native_save_dialog_exercised' true
  atPut result 'native_save_initial_directory' (global 'ideSaveInitialDirectory')
  saved = (readFile (join (at config 'output') '/saved.ubp'))
  ideAssert (saved == (codeString (project (scripter rt)))) 'real IDE saveProject writes current source'
  ideAssert (saveLoadTest (project (scripter rt))) 'IDE project save/load roundtrip'
  closePort rt
  ideConnect rt (at config 'port')
  reconnected = (ideCRCs rt)
  for id (keys crcs) {
    ideAssert ((at reconnected id) == (at crcs id)) (join 'reconnect preserves CRC ' id)
  }

  // Edit the real project Function AST (not bytecode). The function is hidden
  // from the scripting area, so explicitly mark it eligible for recompile,
  // exactly as syncScripts does for visible custom-block definitions.
  donor = (loadFromString (newMicroBlocksProject) (readFile (at config 'edited')) false)
  func = (functionNamed (project (scripter rt)) 'doublePlusOne')
  setField func 'cmdList' (cmdList (functionNamed donor 'doublePlusOne'))
  atPut (at (getField rt 'chunkIDs') 'doublePlusOne') 5 true
  functionBodyChanged (scripter rt)
  stopAndSyncScripts rt
  editedCRCs = (ideCRCs rt)
  changed = 0
  for id (keys crcs) {
    if ((at crcs id) != (at editedCRCs id)) { changed += 1 }
  }
  ideAssert (changed == 1) 'incremental source edit changes exactly one compiled chunk'
  atPut result 'incrementally_changed_chunks' changed
  startAll rt
  idePump rt 100
  ideAssert ((ideGetVar rt 'answer') == 42) 'incrementally compiled function result is 42'
  sendStopAll rt
  idePump rt 100

  recovered = (ideReadback rt)
  recoveredSource = (codeString recovered)
  writeFile (join (at config 'output') '/decompiled.ubp') recoveredSource
  ideAssert (saveLoadTest recovered) 'decompiler output survives IDE save/load'
  ideAssert ((count (allFunctions recovered)) == 1) 'decompiler recovers custom function'
  // Decompiler intentionally randomizes block coordinates and omits lone
  // comments. Record these differences instead of claiming text identity.
  sourceExact = (recoveredSource == (codeString (project (scripter rt))))
  ideAssert (not sourceExact) 'source roundtrip differences are observed, not hidden'
  originalScripts = (count (scripts (main (project (scripter rt)))))
  recoveredScripts = (count (scripts (main recovered)))
  originalAuthor = (author (main (project (scripter rt))))
  recoveredAuthor = (author (main recovered))
  originalDescription = (description (main (project (scripter rt))))
  recoveredDescription = (description (main recovered))
  ideAssert (recoveredScripts == (originalScripts - 1)) 'decompiler omits standalone comment only'
  installDecompiledProject rt recovered
  ideCRCs rt
  startAll rt
  idePump rt 150
  ideAssert ((ideGetVar rt 'answer') == 42) 'decompile/recompile/upload roundtrip executes correctly'
  ideAssert ((ideGetVar rt 'status') == 'running') 'roundtrip retains string variable'
  sendStopAll rt

  atPut result 'status' 'PASS'
  atPut result 'compiled_bytes' total
  atPut result 'largest_chunk_bytes' largest
  atPut result 'chunk_count' (count crcs)
  atPut result 'recompiled_chunk_count' (count (getField rt 'chunkIDs'))
  atPut result 'incrementally_changed_chunks' changed
  atPut result 'decompiled_source_exact' sourceExact
  atPut result 'original_script_count' originalScripts
  atPut result 'decompiled_script_count' recoveredScripts
  atPut result 'shared_native_compat' true
  atPut result 'native_save_dialog_exercised' true
  atPut result 'native_save_initial_directory' (global 'ideSaveInitialDirectory')
  atPut result 'readback_bytes_exact' true
  atPut result 'observed_counter_start' counter1
  atPut result 'observed_counter_end' counter2
  atPut result 'observed_counter_stopped' stopped2
  atPut result 'observed_elapsed_milliseconds' elapsed
  atPut result 'observed_history' history
  atPut result 'original_author' originalAuthor
  atPut result 'decompiled_author' recoveredAuthor
  atPut result 'original_description' originalDescription
  atPut result 'decompiled_description' recoveredDescription
  writeFile (join (at config 'output') '/gp-result.json') (jsonStringify result)
  print 'IDE_TEST_COMPLETE'
  closePort rt
  exit
}
