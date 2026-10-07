// SPDX-License-Identifier: Apache-2.0
// The GP interpreter calls these Javascript serial primitives synchronously.
// Return the number actually accepted; GP's unmodified sendMsg retries on zero.
(() => {
  'use strict';
  // This self-hosted development build is deliberately not an offline PWA.
  // Do not register upstream's missing/stale service worker on window load.
  window.onload = null;
  // GP may finish a long upload after the browser's transient user activation
  // expires. Use the upstream download fallback rather than a native picker
  // that then fails with SecurityError. Project serialization stays upstream.
  window.GP_writeFile = async (data, filename) => {
    filename = filename || 'Untitled.ubp';
    saveAs(new Blob([data]), filename);
    GP.lastSavedFileName = filename;
  };
  const TX_LIMIT = 64 * 1024, RX_LIMIT = 4 * 1024 * 1024;
  let socket = null, opening = null, received = 0, loaded = false;
  let state = 'disconnected', lastError = '', busy = false;
  const stats = {bytesSent: 0, bytesReceived: 0, connections: 0};
  let readyResolve;
  const ready = new Promise(resolve => { readyResolve = resolve; });
  const pendingResults = new Map();
  const readUpstream = window.GP_readSerialPort;
  const $ = id => document.getElementById(id);

  function show(message) {
    if (message !== undefined) $('erbium-status').textContent = message;
    $('erbium-connect').disabled = !loaded || busy || state !== 'disconnected';
    $('erbium-disconnect').disabled = !loaded || busy || state === 'disconnected';
    $('erbium-reset').disabled = !loaded || busy;
    $('erbium-example').disabled = !loaded || busy;
  }
  function clearInput() {
    window.GP_serialInputBuffers = [];
    received = 0;
  }
  function call(endpoint, params = [], timeout = 30000) {
    return ready.then(() => new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`GP API timeout: ${endpoint}`)), timeout);
      GP.apiCall(endpoint, params, result => {
        clearTimeout(timer);
        resolve(result);
      });
    }));
  }
  function openSocket() {
    if (socket && socket.readyState === WebSocket.OPEN) return Promise.resolve();
    if (opening) return opening;
    clearInput();
    lastError = '';
    state = 'connecting';
    show('Opening virtual UART…');
    const url = new URL('/uart', location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(url);
    socket = ws;
    ws.binaryType = 'arraybuffer';
    opening = new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        lastError = 'Virtual UART connection timed out';
        ws.close();
        fail(lastError);
      }, 10000);
      function fail(message) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        opening = null;
        reject(new Error(message));
      }
      ws.onopen = () => {
        if (socket !== ws) { ws.close(); return; }
        settled = true;
        clearTimeout(timer);
        opening = null;
        state = 'connected';
        stats.connections++;
        show('UART open — waiting for real firmware');
        resolve();
      };
      ws.onmessage = event => {
        if (socket !== ws) return;
        if (!(event.data instanceof ArrayBuffer)) {
          lastError = 'UART protocol error: non-binary frame';
          ws.close(1003, 'Binary UART only');
          return;
        }
        const data = new Uint8Array(event.data);
        if (received + data.byteLength > RX_LIMIT) {
          lastError = 'UART input overflow; connection closed (no bytes silently dropped)';
          ws.close(1009, 'UART input overflow');
          return;
        }
        stats.bytesReceived += data.byteLength;
        received += data.byteLength;
        GP_serialInputBuffers.push(data);
      };
      ws.onerror = () => {
        lastError = 'Cannot open UART (board busy, service unavailable, or network error)';
        fail(lastError);
      };
      ws.onclose = event => {
        fail(lastError || `UART closed (${event.code})`);
        if (socket !== ws) return;
        socket = null;
        opening = null;
        state = 'disconnected';
        clearInput();
        show(lastError || (event.code === 1012 ? 'Board restarted — reconnect to restore project' : 'Disconnected'));
        if (loaded) GP.apiCall('ide.updateConnection');
      };
    });
    return opening;
  }
  function closeSocket() {
    const ws = socket;
    if (!ws) {
      state = 'disconnected';
      clearInput();
      show('Disconnected');
      return Promise.resolve();
    }
    return new Promise(resolve => {
      ws.addEventListener('close', resolve, {once: true});
      ws.close(1000, 'IDE disconnect');
    });
  }

  // Override only the browser serial host functions. No navigator mutation,
  // patched compiler, USB permission, CDN, Boardie, or protocol re-framing.
  window.hasWebSerial = () => true;
  window.hasChromeSerial = () => false;
  window.hasWebBluetooth = () => false;
  window.webBluetoothConnected = () => false;
  window.webSerialIsConnected = () => !!socket && socket.readyState === WebSocket.OPEN;
  window.GP_getSerialPorts = () => { window.GP_serialPortNames = []; };
  window.GP_openSerialPort = () => {
    openSocket().catch(error => { lastError = error.message; show(lastError); });
    return 1; // upstream primitive's async-open contract
  };
  window.GP_isOpenSerialPort = window.webSerialIsConnected;
  window.GP_closeSerialPort = () => { void closeSocket(); };
  window.webSerialConnect = window.GP_openSerialPort;
  window.webSerialDisconnect = window.GP_closeSerialPort;
  window.GP_readSerialPort = maxBytes => {
    const bytes = readUpstream(maxBytes);
    received -= bytes.byteLength;
    return bytes;
  };
  window.GP_writeSerialPort = data => {
    if (!window.webSerialIsConnected()) return 0;
    if (socket.bufferedAmount + data.byteLength > TX_LIMIT) return 0;
    socket.send(data); // respects typed-array offset/length, binary frame only
    stats.bytesSent += data.byteLength;
    return data.byteLength;
  };
  window.webSerialWrite = window.GP_writeSerialPort;
  window.GP_setSerialPortDTR = () => {};
  window.GP_setSerialPortRTS = () => {};
  window.GP_setSerialPortDTRandRTS = () => {};

  document.addEventListener('erbium.result', event => {
    const [id, value] = event.detail.value;
    const pending = pendingResults.get(id);
    if (pending) {
      clearTimeout(pending.timer);
      pendingResults.delete(id);
      if (value && value.$erbiumError) pending.reject(new Error(value.$erbiumError));
      else pending.resolve(value);
    }
  });
  async function evalGP(source, timeout = 30000) {
    await ready;
    return new Promise((resolve, reject) => {
      GP.apiCall('erbium.eval', [source]);
      const id = GP.lastCallId;
      const timer = setTimeout(() => {
        pendingResults.delete(id);
        reject(new Error('GP evaluation timed out'));
      }, timeout);
      pendingResults.set(id, {resolve, reject, timer});
    });
  }
  async function connect() {
    await ready;
    // Explicit client connection, with socket open before first firmware ping.
    await openSocket();
    await call('erbium.connect');
    return true;
  }
  async function disconnect() {
    await call('erbium.disconnect');
    await closeSocket();
  }
  async function reset() {
    // Reset is explicit and destructive; no automatic reset on disconnect.
    await disconnect();
    show('Restarting board…');
    const response = await fetch('/api/reset', {method: 'POST'});
    if (!response.ok) throw new Error(`Reset failed: HTTP ${response.status}`);
    const result = await response.json();
    await connect();
    return result;
  }
  async function loadExample() {
    if (!window.confirm('Replace the current project with the Erbium example? Save any changes first.')) return;
    const response = await fetch('/example.ubp');
    if (!response.ok) throw new Error(`Example failed: HTTP ${response.status}`);
    await call('erbium.import', [await response.text(), 'Erbium Showcase.ubp']);
  }
  window.ErbiumIDE = Object.freeze({
    ready, call, evalGP,
    loadGP: (source, timeout) => evalGP(`${source}\ntrue`, timeout),
    launchGP: source => call('erbium.launch', [source]),
    importProject: (source, name = 'project.ubp') => call('erbium.import', [source, name]),
    source: () => call('erbium.source'),
    runtimeState: () => call('erbium.state'),
    backendStatus: async () => {
      const response = await fetch('/api/status');
      if (!response.ok) throw new Error(`Status failed: HTTP ${response.status}`);
      return response.json();
    },
    connect, disconnect, reset,
    transportState: () => ({state, lastError, queuedInput: received,
      bufferedOutput: socket ? socket.bufferedAmount : 0, ...stats})
  });

  for (const [id, action] of [
    ['erbium-connect', connect], ['erbium-disconnect', disconnect], ['erbium-reset', reset],
    ['erbium-example', loadExample]
  ]) {
    $(id).addEventListener('click', async () => {
      busy = true; show();
      try { await action(); } catch (error) { lastError = error.message; show(lastError); }
      finally { busy = false; show(); }
    });
  }
  document.addEventListener('ready', () => {
    // Keep the upstream connection widget, but do not offer USB/BLE/Boardie:
    // this build always runs the real backend interpreter through /uart.
    Menus.connection.items = [
      {label: 'connect (Erbium UART)', action: () => $('erbium-connect').click(),
        hidden: () => IDE.board.connected},
      {label: 'disconnect', action: () => $('erbium-disconnect').click(),
        hidden: () => !IDE.board.connected}
    ];
    Buttons.connect.description = 'Connect to the real Erbium virtual UART.';
    loaded = true;
    readyResolve(window.ErbiumIDE);
    show('Ready — connect to the Erbium interpreter');
  }, {once: true});
  document.addEventListener('board.connected', event => {
    if (event.detail.value) show('Connected to Erbium — real interpreter');
  });
  window.addEventListener('pagehide', () => { if (socket) socket.close(); });
})();
