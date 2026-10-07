// SPDX-License-Identifier: MPL-2.0
// The pinned IDE sources have an HTML toolbar/API; the pinned Linux GP is
// a real SDL native executable without those newer JS primitives. Restore
// native widgets around the unchanged MicroBlocks editor/compiler/runtime.
to browserURL { return '' }
to browserIsChromeOS { return false }
to browserHasWebSerial { return false }
to browserHasLanguage code { return false }
to browserGetDroppedFile { return nil }
to browserGetDroppedText { return nil }
to browserLastSaveName { return nil }
to browserElectronOS { return 3 }
to browserReadPrefs {
	return (readFile '/var/lib/smallvm-ide/user-prefs.json')
}
to browserWritePrefs data {
	writeFile '/var/lib/smallvm-ide/user-prefs.json' data
}
to browserWriteFile data suggestedFileName id {
	path = (microBlocksFileToWrite (join (microblocksFolder) '/' suggestedFileName))
	if (and (notNil path) (path != '')) { writeFile path data }
}
method processLastCall MicroBlocksAPI { }
method notify MicroBlocksAPI event value { }
method setProperty MicroBlocksAPI path value {
	if (isNil (global 'nativeIDEProperties')) {
		setGlobal 'nativeIDEProperties' (dictionary)
	}
	atPut (global 'nativeIDEProperties') path value
}

method menuFor MicroBlocksAPI items callback class {
	nativeMenu = (menu nil)
	for item items {
		label = (at item 1)
		if (isClass callback 'Action') {
			itemAction = (action 'call' callback label)
		} else {
			itemAction = (at item 2)
		}
		addItem nativeMenu label itemAction
	}
	popUpAtHand nativeMenu (global 'page')
}

// The upstream script-area right-click forwards to its HTML menu. Reuse GP's
// existing native menu instead; block/input-slot menus already remain native.
method rightClicked ScriptEditor aHand {
	popUpAtHand (contextMenuForGP this) (page aHand)
	return true
}

// Prompter widgets are still supplied by the GP library, but Page's entry
// points have been redirected to HTML. Restore those native entry points.
method prompt Page question default editRule callback details {
	if (isNil editRule) { editRule = 'line' }
	p = (new 'Prompter')
	initialize p (localized question) (localized default) editRule callback details
	fixLayout p
	setPosition (morph p) (half ((width morph) - (width (morph p)))) (40 * (global 'scale'))
	addPart morph (morph p)
	edit (textBox p) hand
	selectAll (textBox p)
	if (isNil callback) {
		cancelTouchHold hand
		while (not (isDone p)) { doOneCycle this }
		destroy (morph p)
		return (answer p)
	}
}

method confirm Page title question yesLabel noLabel callback {
	p = (new 'Prompter')
	initializeForConfirm p title question yesLabel noLabel callback
	fixLayout p
	setPosition (morph p) (half ((width morph) - (width (morph p)))) (100 * (global 'scale'))
	addPart morph (morph p)
	if (isNil callback) {
		cancelTouchHold hand
		while (not (isDone p)) { doOneCycle this }
		return (answer p)
	}
}

method inform Page details title yesLabel nonBlocking {
	p = (new 'Prompter')
	initializeForInform p title details yesLabel
	fixLayout p
	setPosition (morph p) (half ((width morph) - (width (morph p)))) (100 * (global 'scale'))
	addPart morph (morph p)
	if (true != nonBlocking) {
		cancelTouchHold hand
		while (not (isDone p)) { doOneCycle this }
	}
}

// The current picker names browser virtual roots ("Downloads", "Libraries")
// and its Computer shortcut invokes browserReadFile. Resolve those roots to
// native directories, keeping the same actual picker widgets and callbacks.
method showFolder MicroBlocksFilePicker path isTop {
	if (path == 'Examples') { path = '../Examples' }
	if (path == 'Libraries') { path = '../Libraries' }
	if (path == 'Downloads') {
		path = (join (userHomePath) '/Downloads')
		makeDirectory path
	}
	useEmbeddedFS = false
	currentDir = path
	if isTop { topDir = path }
	setText folderReadout (localizeDir this (filePart path))
	updateParentAndNewFolderButtons this
	setCollection (contents listPane) (folderContents this)
	changeScrollOffset listPane -100000 -100000
	if (notNil onFolderSelect) { call onFolderSelect path }
}

method setComputer MicroBlocksFilePicker { showFolder this '/' true }

// Running highlights and drag shadows use Canvas-only primitives upstream.
// Render the same silhouette as a native bitmap instead, before attaching it
// to the target (so fullCostume cannot recursively include this new effect).
method initialize ShadowEffect aBlock effectType {
	morph = (newMorph this)
	acceptEvents morph false
	targetBlock = aBlock
	scale = (global 'scale')
	target = (morph aBlock)
	r = (fullBounds target)
	source = (fullCostume target)
	if (effectType == 'highlight') {
		color = (microBlocksColor 'scriptRunning')
		padding = (4 * scale)
		silhouette = (newBitmap (width source) (height source) color)
		applyMask silhouette source
		bitmap = (newBitmap ((width source) + (2 * padding)) ((height source) + (2 * padding)))
		for x (array 0 padding (2 * padding)) {
			for y (array 0 padding (2 * padding)) {
				drawBitmap bitmap silhouette x y
			}
		}
		setCostume morph bitmap
		setPosition morph ((left r) - padding) ((top r) - padding)
	} else {
		bitmap = (newBitmap (width source) (height source) (gray 0 60))
		applyMask bitmap source
		setCostume morph bitmap
		setPosition morph ((left r) + (7 * scale)) ((top r) + (7 * scale))
	}
	return this
}

method drawOn ShadowEffect ctx { drawCostumeOn morph ctx }

to readSVGIcon iconName fgColor bgColor iconScale strokeOverideFlag {
	fileName = (join 'img/' iconName '.svg')
	data = (readFile (join '../' fileName))
	if (isNil data) { data = (readEmbeddedFile fileName) }
	return (renderSVG data fgColor bgColor iconScale strokeOverideFlag)
}

method fixScripterLayout MicroBlocksEditor {
	if (isNil scripter) { return }
	pageM = (morph (global 'page'))
	m = (morph scripter)
	setPosition m 0 88
	setExtent m (width pageM) (max 1 ((height pageM) - 88))
	fixLayout scripter
}

// Keep the pinned IDE deterministic/offline rather than fetching a newer build.
method checkLatestVersion MicroBlocksEditor {
	newerVersion = nil
	isPilot = false
}

method closeAllDialogs MicroBlocksEditor {
	pageM = (morph (global 'page'))
	for p (copy (parts pageM)) {
		if (and (p != morph)
				(not (isClass (handler p) 'SmallvmNativeToolbar'))) {
			removePart pageM p
		}
	}
	doOneCycle (global 'page')
}

// Native save dialog and disk write instead of browserWriteFile's native no-op.
method saveProject MicroBlocksEditor fName {
	saveScripts scripter nil true
	if (or (isNil fName) ('' == fName)) {
		fName = (join (microblocksFolder) '/Untitled.ubp')
	}
	fName = (microBlocksFileToWrite fName '.ubp')
	if (or (isNil fName) ('' == fName)) { return }
	if (not (canWriteProject this fName)) { return }
	writeFile fName (codeString (project scripter))
	fileName = fName
	updateTitle this
}

defineClass SmallvmNativeToolbar morph statusLabel previousStatus lastUpdate

method initialize SmallvmNativeToolbar editor {
	morph = (newMorph this)
	setCostume morph (newBitmap 1280 88 (color 29 47 64))
	setFPS morph 10
	x = 8
	for entry (array
		(array 'File' 'projectMenu' editor)
		(array 'Connect' 'connectionMenu' this)
		(array 'Start' 'startAll' editor)
		(array 'Stop' 'stopAndSyncScripts' editor)
		(array 'Settings' 'settingsMenu' editor)
		(array '−' 'zoomOut' editor)
		(array '+' 'zoomIn' editor)
	) {
		button = (newButton (at entry 1) (action (at entry 2) (at entry 3)))
		addPart morph (morph button)
		setPosition (morph button) x 7
		x += ((width (morph button)) + 6)
	}
	statusLabel = (newText 'Connecting to Erbium…' 'Arial' 14 (color 255 230 150))
	addPart morph (morph statusLabel)
	setPosition (morph statusLabel) x 17
	x = 8
	for category (categories (scripter editor)) {
		label = (substring category 5)
		button = (newButton label (action 'selectCategory' (scripter editor) category))
		addPart morph (morph button)
		setPosition (morph button) x 49
		x += ((width (morph button)) + 6)
	}
	button = (newButton 'Libraries…' (action 'libraryMenu' this))
	addPart morph (morph button)
	setPosition (morph button) x 49
	return this
}

method libraryMenu SmallvmNativeToolbar {
	scripter = (scripter (findMicroBlocksEditor))
	menu = (menu 'Libraries')
	addItem menu 'Add library…' (action 'importLibrary' scripter)
	addLine menu
	for name (sorted (keys (libraries (project scripter)))) {
		addItem menu name (action 'selectLibrary' scripter name)
	}
	popUpAtHand menu (global 'page')
}

method connectionMenu SmallvmNativeToolbar {
	runtime = (smallRuntime)
	menu = (menu 'Erbium serial')
	addItem menu 'Connect to Erbium emulator' (action 'setPort' runtime '/run/smallvm-ide/erbium-pts')
	addItem menu 'Disconnect IDE' (action 'setPort' runtime 'disconnect')
	addItem menu 'Enter port name…' (action 'setPort' runtime 'other...')
	popUpAtHand menu (global 'page')
}

method step SmallvmNativeToolbar {
	if (true == (global 'nativeConnecting')) { return }
	setGlobal 'nativeConnecting' true
	request = (readFile '/run/smallvm-ide/ide-request.txt')
	if (notNil request) {
		deleteFile '/run/smallvm-ide/ide-request.txt'
		if (request == 'disconnect') {
			closePort (smallRuntime)
			setField (smallRuntime) 'disconnected' true
		} (request == 'reconnect') {
			setPort (smallRuntime) '/run/smallvm-ide/erbium-pts'
		}
	}
	state = (updateConnection (smallRuntime))
	props = (global 'nativeIDEProperties')
	name = (at props 'project.title')
	if (or (isNil name) ('' == name)) { name = 'Untitled' }
	if (state == 'connected') {
		status = (join '● Erbium connected   ·   ' name)
		setColor statusLabel (color 145 238 162)
	} else {
		status = (join '○ Erbium not connected   ·   ' name)
		setColor statusLabel (color 255 230 150)
	}
	if (previousStatus != status) {
		setText statusLabel status
		previousStatus = status
	}
	info = (dictionary)
	atPut info 'connection' state
	atPut info 'project' name
	atPut info 'vmVersion' (vmVersion (smallRuntime))
	atPut info 'serialPath' '/run/smallvm-ide/erbium-pts'
	writeFile '/run/smallvm-ide/ide-status.json' (jsonStringify info)
	setGlobal 'nativeConnecting' false
}
