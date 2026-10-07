// SPDX-License-Identifier: Apache-2.0
// Transport-only unit tests. Real compiler/firmware tests live in ../tests/.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function fixture() {
  const elements = new Map();
  const handlers = new Map();
  const frames = [];
  const sockets = [];
  const requests = [];
  let ctx;
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        textContent: '', disabled: true,
        addEventListener(type, handler) { this[type] = handler; },
        click() { return this.clickHandler ? this.clickHandler() : this['click']?.(); }
      });
      return elements.get(id);
    },
    addEventListener(type, handler) { handlers.set(type, handler); }
  };
  class FakeSocket {
    static OPEN = 1;
    constructor(url) {
      this.url = String(url);
      this.readyState = 0;
      this.bufferedAmount = 0;
      this.listeners = new Map();
      sockets.push(this);
      queueMicrotask(() => {
        this.readyState = 1;
        this.onopen();
      });
    }
    send(data) { frames.push(Uint8Array.from(data)); }
    addEventListener(type, handler) { this.listeners.set(type, handler); }
    close(code = 1000, reason = '') {
      this.readyState = 3;
      this.closeCode = code;
      this.closeReason = reason;
      queueMicrotask(() => {
        this.onclose({code});
        this.listeners.get('close')?.();
      });
    }
  }
  ctx = {
    console, setTimeout, clearTimeout, queueMicrotask,
    Uint8Array, ArrayBuffer, URL, Map, WebSocket: FakeSocket,
    location: {href: 'https://example.test/', protocol: 'https:'}, document,
    GP_serialInputBuffers: [], GP_serialPortNames: [],
    GP: {
      apiCall(endpoint, params, callback = () => {}) {
        if (endpoint === 'erbium.disconnect') ctx.GP_closeSerialPort();
        queueMicrotask(() => callback(true));
      }
    },
    GP_readSerialPort(maxBytes) {
      const chunks = ctx.GP_serialInputBuffers;
      const all = new Uint8Array(chunks.reduce((n, x) => n + x.length, 0));
      let offset = 0;
      for (const bytes of chunks) { all.set(bytes, offset); offset += bytes.length; }
      ctx.GP_serialInputBuffers = all.length > maxBytes ? [all.slice(maxBytes)] : [];
      return all.slice(0, maxBytes);
    },
    Menus: {connection: {items: []}}, Buttons: {connect: {}}, IDE: {board: {}},
    fetch: async (url, options = {}) => {
      requests.push([url, options]);
      return {ok: true, json: async () => ({emulator_running: true})};
    },
    addEventListener() {}
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'erbium.js'), 'utf8'), ctx);
  handlers.get('ready')();
  return {ctx, sockets, frames, requests, elements};
}

test('raw binary UART, view bounds, ordered reads and send backpressure', async () => {
  const {ctx, sockets, frames} = fixture();
  await ctx.ErbiumIDE.connect();
  const ws = sockets[0];
  assert.equal(ws.url, 'wss://example.test/uart');
  assert.equal(ws.binaryType, 'arraybuffer');
  assert.equal(frames.length, 0, 'no application greeting/control frames');
  const data = new Uint8Array([99, 250, 1, 0, 88]);
  assert.equal(ctx.GP_writeSerialPort(data.subarray(1, 4)), 3);
  assert.deepEqual([...frames[0]], [250, 1, 0]);
  ws.bufferedAmount = 65536;
  assert.equal(ctx.GP_writeSerialPort(new Uint8Array([1])), 0);
  assert.equal(frames.length, 1, 'backpressure does not claim an unsent write');
  ws.onmessage({data: new Uint8Array([1, 2]).buffer});
  ws.onmessage({data: new Uint8Array([3, 4, 5]).buffer});
  assert.deepEqual([...ctx.GP_readSerialPort(3)], [1, 2, 3]);
  assert.equal(ctx.ErbiumIDE.transportState().queuedInput, 2);
  assert.deepEqual([...ctx.GP_readSerialPort(999)], [4, 5]);
  assert.equal(ctx.ErbiumIDE.transportState().queuedInput, 0);
  await ctx.ErbiumIDE.disconnect();
  assert.equal(ctx.ErbiumIDE.transportState().state, 'disconnected');
});

test('reset is HTTP POST, disconnects and reconnects without UART control bytes', async () => {
  const {ctx, sockets, frames, requests} = fixture();
  await ctx.ErbiumIDE.connect();
  sockets[0].onmessage({data: new Uint8Array([42]).buffer});
  await ctx.ErbiumIDE.reset();
  assert.equal(sockets.length, 2);
  assert.equal(sockets[0].readyState, 3);
  assert.deepEqual(requests.map(([url, opt]) => [url, opt.method]), [['/api/reset', 'POST']]);
  assert.equal(frames.length, 0);
  assert.equal(ctx.ErbiumIDE.transportState().queuedInput, 0);
  await ctx.ErbiumIDE.disconnect();
});

test('non-binary frames close with a visible protocol error', async () => {
  const {ctx, sockets, elements} = fixture();
  await ctx.ErbiumIDE.connect();
  sockets[0].onmessage({data: '{"not":"UART"}'});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sockets[0].closeCode, 1003);
  assert.match(elements.get('erbium-status').textContent, /non-binary/);
});

test('bounded input closes loudly rather than dropping UART bytes', async () => {
  const {ctx, sockets, elements} = fixture();
  await ctx.ErbiumIDE.connect();
  sockets[0].onmessage({data: new ArrayBuffer(4 * 1024 * 1024 + 1)});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sockets[0].closeCode, 1009);
  assert.match(elements.get('erbium-status').textContent, /overflow/);
  assert.equal(ctx.ErbiumIDE.transportState().queuedInput, 0);
});
