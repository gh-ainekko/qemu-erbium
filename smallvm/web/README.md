# Self-hosted MicroBlocks browser IDE

This is the real upstream MicroBlocks IDE, **not noVNC, Boardie, a mock compiler,
or a browser-side Erbium emulator**. Its GP interpreter and GUI run in WASM in
the browser. Its original MicroBlocks compiler produces bytecode for the
SmallVM firmware running in the backend's real `erbium_emu` process.

## Live service and use

Open `https://erbium-qemu.exe.xyz/`. Click **Load example**, accept replacing
the current project, then **Connect** and the yellow **Start** triangle.
Choose **Variables** and click **answer**: it reports **41** from the board.
Use the project menu's **Save** to download a `.ubp` file to your computer;
open/import your own project with that menu or drag the file into the editor.
Disconnect or close the tab before connecting from a second tab.

After building the emulator, firmware and browser assets:

```sh
scripts/setup-smallvm-web.sh
# Explicitly migrate an existing native/noVNC installation:
scripts/setup-smallvm-web.sh --replace-desktop
systemctl status smallvm-web.service
curl http://localhost:8000/api/status
sudo journalctl -u smallvm-web.service -n 50
```

The enabled `smallvm-web.service` runs only Python/aiohttp and sysemu, not
Xvfb, a native GP process, VNC or websockify. It listens on loopback port 8000
behind the **private authenticated exe.dev proxy**. Keep that proxy private:
origin checks are not authentication. Local shell users are trusted.
Static navigation from other sites is allowed; cross-origin UART/API control
is rejected. The unit restricts filesystem writes to its state directory.
Rotating diagnostic/UART logs live in `/var/lib/smallvm-web/current/` and are
not served as web assets. Fatal backend I/O errors stop/reap the emulator and
exit unsuccessfully so systemd can restart the service.

**Reset board** restarts the firmware and reconnects the current editor;
ordinary Disconnect leaves the emulator running. Neither operation saves a
project durably on the board. To stop the service:
`sudo systemctl disable --now smallvm-web.service`.
The old `smallvm-ide.service` is stopped and disabled on this VM; its optional
implementation and saved files have not been deleted.

## Build and provenance

From the repository root, on Ubuntu 24.04:

```sh
sudo apt-get install --no-install-recommends emscripten clang-14 llvm-14 lld-14
scripts/fetch-smallvm-web.sh
```

Source is the existing Codeberg `MicroBlocks/smallvm` checkout, upstream base
`49f337529294640def7b27a2ee1c4e7ba88ccdbb`, with this repository's patches 1–6.
The builder refuses anything other than patched Git tree
`73844f74e19e41eb466ea0c727a85594902be4c1`, regardless of git-am commit IDs.
`scripts/fetch-smallvm.sh` retains its dirty-checkout/no-overwrite policy.
No moving production-site `.wasm` or release assets are fetched.

The build follows `chromeApp/emscripten/buildEmcc.sh` from that source:
compile the GP C interpreter and preload `gp/runtime`, `ide/*.gp`, libraries,
examples, images and translations. `MicroBlocksCompiler.gp`,
`MicroBlocksDecompiler.gp`, runtime UART framing/parser and application startup
are untouched. The full upstream API dispatcher is renamed in the **staged
copy only**; `bridge.gp` forwards its existing endpoints and adds `erbium.*`.
`index.html` is upstream `chromeApp/webapp/microblocks.html` with an Erbium
toolbar, local asset base, no devtools/HTTPS redirect, and the UART adapter.

Pinned toolchain: Emscripten **3.1.6**, clang/LLVM/lld **14.0.6**.
Ubuntu's packaged emcc defaults to clang 15; that combination renamed GP's
argc/argv entrypoint and silently produced a 66-byte WASM without the
interpreter. The script explicitly selects clang 14 and exports `_main`;
`build.py` also rejects an implausibly small WASM. It uses a writable project
cache, not a sudo build or changes to `/usr/share/emscripten`.
`SOURCE_DATE_EPOCH=1791323672` (the pinned base commit's timestamp) fixes GP's
`__DATE__`/`__TIME__` version strings for byte-identical payload rebuilds;
the builder derives explicit macro definitions for this older clang.
Emscripten's random file-package cache UUID is replaced by a UUID derived
from the data payload hash; no compiled GP/WASM code is changed by this step.

Emscripten's zlib port is **1.2.11**, with archive SHA-512
`a42b8359e76cf7b3ae70bf31f0f8a8caa407ac80e8fe08b838076cd5e45ac2e685dae45eb59db2d25543fb3b5bd13b843a02bb8373cda704d7238be50d5e9c68`,
verified by the pinned emcc port fetcher. Firmware flasher images are
deliberately omitted; flashing unrelated physical boards is unsupported.

Outputs are in `build/smallvm/web` (override with `SMALLVM_WEB_BUILD`).
`provenance.json` records source identity, toolchain, command and overlay
hashes; `SHA256SUMS` inventories all served assets. `wasm.sha256` in this
directory pins the three generated runtime payloads. The build checks those
hashes rather than updating the lock automatically. To change a deliberate
overlay, rebuild with `SMALLVM_WEB_UPDATE_LOCK=1`, inspect the diff, then update
the tracked lock. The script leaves its exported source stage for inspection.

Upstream assets/GP changes retain MPL-2.0. New JS/build scripts are Apache-2.0.
The asset directory includes upstream license notices and the zlib notice.

## Backend contract and layout

`server.py` and its service/setup are maintained separately from the client.
It serves `/` from `smallvm/web/index.html`, `/erbium.js` from this directory,
and `/assets/` from `build/smallvm/web`; no external CDN is needed.

* `/uart`: same-origin WebSocket (`ws:` locally, `wss:` over HTTPS), **binary
  messages only**, each carrying raw ordered UART bytes. No greeting, JSON
  envelope, extra control frames or subprotocol. One exclusive client; a
  second upgrade fails with HTTP 409.
* `/api/status`: JSON status, including `connected`, `emulator_running`,
  `emulator_restarts`, byte totals and queue counts.
* `POST /api/reset`: close the wire (1012), clear queues and restart firmware;
  respond with status. Reset is not encoded into UART bytes.

The adapter replaces only upstream Javascript serial host functions,
not `navigator.serial`, the GP compiler, serial framing, or message handlers.
It copies incoming bytes into the existing `GP_serialInputBuffers` and retains
upstream `GP_readSerialPort`. Writes return the exact byte count accepted, or
zero when the WebSocket's 64-KiB send high-water mark is reached; upstream GP
send logic retries after yielding. Input is bounded at 4 MiB and closes loudly
on overflow instead of silently dropping bytes. Reconnect clears stale input.

Connect opens the socket before selecting `webserial` in the real runtime.
Disconnect closes the port without resetting firmware. Reset disconnects,
awaits the HTTP reset and reconnects; the open browser project is then
resynchronized by the upstream runtime. **Board program storage is volatile.**
Use the upstream project menu to save/open `.ubp` files in the browser.
The stock connection widget is retained but offers only Erbium, not Boardie.
Physical peripherals and other board firmware installers are unsupported.

## Shared browser API / real-compiler testing

`window.ErbiumIDE` is available before WASM starts. Wait for its `ready` Promise
before calling GP. The unchanged upstream `GP.apiCall` queue is the bridge,
not a second compiler or a raw synthetic UART test path.

```js
await ErbiumIDE.ready;
await ErbiumIDE.connect();
await ErbiumIDE.importProject(ubpText, 'test.ubp'); // upstream openProject
await ErbiumIDE.call('ide.startAll');             // real compile/sync/start
const state = await ErbiumIDE.runtimeState();     // Erbium, vmVersion 416
const savedText = await ErbiumIDE.source();       // upstream codeString
await ErbiumIDE.disconnect();
await ErbiumIDE.reset();                         // destructive + reconnect
```

`connect()` and mutating calls acknowledge initiation, not completion. Poll
`runtimeState()` until `connected && !connecting && vmVersion >= 300` before
firmware assertions. `call(endpoint, params=[], timeout=30000)` wraps upstream
callbacks in a Promise. Standard upstream endpoint semantics are unchanged
(many acknowledge before doing work).

Additional hooks for actual integration drivers:

* `evalGP(source, timeout=30000)`: evaluate GP text in the running IDE's real
  top-level module, return a JSON-compatible value; GP/serialization errors
  reject the Promise. Dictionary keys must be strings. Do not return compiler
  `chunkIDs` directly: some keys are GP Block objects.
* `loadGP(source)`: define extra driver functions/methods, discard the final
  declaration's result and return `true`. Does **not** replace startup.
* `launchGP(source)`: launch a cooperative task on the existing page; returns
  an acknowledgment. A driver should yield with `waitMSecs`, never enter a
  new blocking native GUI loop or call `exit`.
* GP `browserPostMessage` can report driver results to a browser `message`
  listener. `erbium.result` CustomEvents are reserved for `evalGP` results.
* `backendStatus()` reads the HTTP status; `transportState()` exposes client
  state, error, byte totals and bounded queue sizes without injecting frames.

For CRCs, readback and variable values, driver code should invoke the original
`SmallRuntime` methods and parser (as the regression does), not manufacture
bytecode. Hooks execute trusted GP in the browser only; they do not offer
backend shell execution.

See `../tests/web_ide.md` and `scripts/test-smallvm-web.sh` for the isolated
Playwright real-compiler regression. Do not test against/reset the live port
8000; use a separately owned loopback service/emulator.

`node --test smallvm/web/test_client.cjs` runs four small transport-only unit
checks (raw view bounds/order, backpressure, HTTP reset and bounded/error input).
Those mocks are not used by the real compiler/firmware browser regression.
