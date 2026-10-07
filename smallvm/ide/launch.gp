// SPDX-License-Identifier: MPL-2.0
// Native IDE launch glue; load AFTER the pinned, unmodified loadIDE.gp.
// The upstream openPortAndSendPing closes (and clears) the selected port.
// Preserve it, and accept only our stable serial symlink in addition to the
// upstream enumerated ports and /dev/pts paths.
method openPortAndSendPing SmallRuntime {
	selectedPort = portName
	closePort this
	portName = selectedPort
	connectionStartTime = (msecsSinceStart)
	ensurePortOpen this
	if (notNil port) { readSerialPort port true }
	lastPingRecvMSecs = 0
	sendMsg this 'pingMsg'
}

method ensurePortOpen SmallRuntime {
	if (true == disconnected) { return }
	if (isWebSerial this) { return }
	if (or (isNil port) (not (isOpenSerialPort port))) {
		if (and (notNil portName)
			(or (portName == '/run/smallvm-ide/erbium-serial')
				(contains (portList this) portName)
				(notNil (findSubstring 'pts' portName)))) {
			port = (safelyRun (action 'openSerialPort' portName 115200))
			if (not (isClass port 'Integer')) { port = nil }
			if (isNil port) { return }
			disconnected = false
			waitMSecs 100
		}
	}
}

// openMicroBlocksEditor enters a blocking morphic loop, so this startup uses
// its normal initialization sequence and selects the port before that loop.
to startup {
	page = (newPage 1280 800)
	setDevMode page false
	toggleMorphicMenu (hand page) false
	setGlobal 'page' page
	open page true 'MicroBlocks — Erbium native IDE'
	editor = (initialize (new 'MicroBlocksEditor') (emptyProject))
	addPart page editor
	redrawAll page
	readVersionFile (smallRuntime)
	applyUserPreferences editor
	developerModeChanged editor
	notify (api (smallRuntime)) 'ready'
	addPart page (initialize (new 'SmallvmNativeToolbar') editor)
	launchPath = (last (commandLine))
	repo = (substring launchPath 1 ((count launchPath) - (count '/smallvm/ide/launch.gp')))
	examples = (join repo '/smallvm/examples')
	setField editor 'lastProjectFolder' examples
	demo = (join examples '/Erbium Showcase.ubp')
	if (notNil (readFile demo)) {
		print 'Opening showcase:' demo
		openProjectFromFile editor demo
	}
	setPort (smallRuntime) '/run/smallvm-ide/erbium-serial'
	startSteppingSafely page
}
