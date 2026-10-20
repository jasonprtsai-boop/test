const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function runtime(fetch) {
  const timers = new Map();
  let sequence = 0;
  const status = {hidden: true, textContent: ''};
  const context = vm.createContext({
    fetch, AbortController, console: {error() {}},
    document: {getElementById: () => status},
    window: {addEventListener() {}},
    setTimeout: (fn, ms) => { const id = ++sequence; timers.set(id, {fn, ms}); return id; },
    clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/common.js'), 'utf8') + '\nthis.api = TMChess;', context);
  return {api: context.api, timers, status};
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test('a hung request times out, clears timer, and never repeats a command', async () => {
  let calls = 0;
  const {api, timers} = runtime((_url, options) => {
    calls++;
    return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  });
  const result = api.request('/api/game/start', {method: 'POST'});
  const rejection = assert.rejects(result, /命令可能已被接收/);
  [...timers.values()][0].fn();
  await rejection;
  assert.equal(calls, 1);
  assert.equal(timers.size, 0);
});

test('polling recovers from failure and never overlaps requests', async () => {
  let calls = 0;
  let resolve;
  const r = runtime(() => {
    calls++;
    if (calls === 1) return Promise.reject(new Error('offline'));
    return new Promise(done => { resolve = done; });
  });
  const states = [];
  const close = r.api.connect(state => states.push(state));
  await flush();
  assert.equal(r.status.hidden, false);
  const [id, timer] = [...r.timers].find(([, timer]) => timer.ms === 1000);
  r.timers.delete(id);
  timer.fn();
  await flush();
  assert.equal(calls, 2);
  assert.equal([...r.timers.values()].filter(t => t.ms === 1000).length, 0);
  resolve({ok: true, json: async () => ({revision: 5})});
  await flush();
  assert.equal(states[0].revision, 5);
  assert.equal(r.status.hidden, true);
  close();
  assert.equal(r.timers.size, 0);
});

test('invalid JSON is a failure rather than a fake successful command', async () => {
  const {api, timers} = runtime(async () => ({ok: true, json: async () => {throw new SyntaxError('invalid JSON');}}));
  await assert.rejects(api.request('/api/state'), /invalid JSON/);
  assert.equal(timers.size, 0);
});

test('player releases buttons after command and refresh fail; stop bypasses pending', async () => {
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      addEventListener(type, fn) {this[type] = fn;},
      hidden: false, disabled: false, value: 'human_red',
    });
    return elements.get(selector);
  };
  const state = {
    phase: 'PLAYER_TURN', first_actor: 'player', side_to_move: 'w',
    controls: {can_hint: true, can_player_done: true, can_stop: true},
    preflight: {ready: true, checks: []}, analysis: {status: 'idle'},
  };
  let rejectHint;
  let hintCalls = 0;
  let stopCalls = 0;
  const requests = [];
  const context = vm.createContext({
    document: {querySelector: element, querySelectorAll: () => []}, window: {},
    TMChess: {
      newestState: (_previous, incoming) => incoming,
      connect: render => render(state),
      request: async url => {
        requests.push(url);
        if (url.endsWith('reset-page')) return state;
        if (url.endsWith('/stop')) {stopCalls++; return state;}
        if (url.endsWith('/hint')) {
          hintCalls++;
          return new Promise((_resolve, reject) => {rejectHint = reject;});
        }
        throw new Error('refresh offline');
      },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/player.js'), 'utf8'), context);
  await flush();
  assert.equal(requests.length, 0, 'opening the page must not reset a live or resumable game');
  const command = element('#hintButton').click();
  assert.equal(element('#hintButton').disabled, true);
  assert.equal(element('#stopButton').disabled, false);
  await element('#hintButton').click();
  assert.equal(hintCalls, 1);
  await element('#stopButton').click();
  assert.equal(stopCalls, 1);
  rejectHint(new Error('command timeout'));
  await command;
  assert.equal(element('#hintButton').disabled, false);
  assert.equal(element('#errorBox').textContent, 'command timeout');
});

test('late command responses cannot roll back the rendered state', () => {
  const {api} = runtime(async () => {});
  const current = {instance_id: 'server-a', revision: 12, phase: 'STOPPED'};
  const older = {instance_id: 'server-a', revision: 11, phase: 'ROBOT_MOVING'};
  assert.equal(api.newestState(current, older), current);
});

test('a backend restart accepts new revisions and rejects responses from the retired process', () => {
  const {api} = runtime(async () => {});
  const previous = {instance_id: 'server-a', revision: 200};
  const restarted = {instance_id: 'server-b', revision: 1};
  assert.equal(api.newestState(previous, restarted), restarted);
  assert.equal(api.newestState(restarted, previous), restarted);
});
