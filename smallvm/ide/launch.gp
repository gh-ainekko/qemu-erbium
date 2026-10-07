// SPDX-License-Identifier: MPL-2.0
// UI startup only. Transport methods come from the real upstream IDE with
// the parent's patch04. The stable alias contains "pts", so upstream native
// ensurePortOpen accepts it without another method override.
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
	setPort (smallRuntime) '/run/smallvm-ide/erbium-pts'
	startSteppingSafely page
}
