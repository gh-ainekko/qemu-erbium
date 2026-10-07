// SPDX-License-Identifier: MPL-2.0
// Browser-only outer API bridge. The upstream compiler, UART protocol, project
// loader and dispatcher are retained, not implemented again in Javascript.

method dispatchCall MicroBlocksAPI request {
	endPoint = (at request 2)
	if (not (beginsWith endPoint 'erbium.')) {
		erbiumUpstreamDispatchCall this request
		return
	}
	id = (at request 1)
	params = (jsonParse (at request 3))
	rt = (smallRuntime)
	editor = (findMicroBlocksEditor)
	if (endPoint == 'erbium.eval') {
		// Remove the request before eval can yield, to avoid running it twice.
		// Delivery uses an explicit result notification keyed by this call ID.
		respondAPIRequest this id 0
		result = (safelyRun (action 'erbiumEvaluate' (at params 1)) (action 'erbiumEvaluationError'))
		notify this 'erbium.result' (array id result)
	} (endPoint == 'erbium.launch') {
		respondAPIRequest this id 0
		launch (global 'page') (newCommand 'eval' (at params 1) nil (topLevelModule))
	} (endPoint == 'erbium.import') {
		respondAPIRequest this id 0
		openProject editor (at params 1) (at params 2) false
	} (endPoint == 'erbium.connect') {
		// Javascript opens the socket first. This selects that existing port
		// without calling the async upstream USB device-picker path.
		setField rt 'portName' 'webserial'
		setField rt 'port' 1
		setField rt 'disconnected' false
		setField rt 'connectionStartTime' (msecsSinceStart)
		setField rt 'lastPingRecvMSecs' 0
		sendMsg rt 'pingMsg'
		respondAPIRequest this id true
	} (endPoint == 'erbium.disconnect') {
		respondAPIRequest this id 0
		closePort rt
	} (endPoint == 'erbium.state') {
		info = (dictionary)
		atPut info 'connected' (connectedToBoard rt)
		atPut info 'vmVersion' (vmVersion rt)
		atPut info 'boardType' (getField rt 'boardType')
		atPut info 'connecting' (notNil (getField rt 'connectionStartTime'))
		atPut info 'chunkCount' (count (getField rt 'chunkIDs'))
		respondAPIRequest this id info
	} (endPoint == 'erbium.source') {
		respondAPIRequest this id (codeString (project (scripter rt)))
	} else {
		respondAPIRequest this id 'Unknown Erbium API endpoint'
	}
}

to erbiumEvaluate source {
	result = (eval source nil (topLevelModule))
	// Surface unsupported return values as errors, not a hung browser Promise.
	jsonStringify result
	return result
}

to erbiumEvaluationError task {
	info = (dictionary)
	atPut info '$erbiumError' (errorReason task)
	return info
}
