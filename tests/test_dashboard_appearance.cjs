// Preference behavior is independent of data refreshes and repository settings.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const script = fs.readFileSync('src/eutonos/dashboard_static/appearance.js', 'utf8');
function start(saved, blocked = false) {
  const handlers = {}, root = {dataset: {}}, control = {value: '', addEventListener: (event, fn) => handlers[event] = fn};
  const writes = [];
  const context = {
    document: {documentElement: root, getElementById: () => control, querySelectorAll: () => [], addEventListener: (event, fn) => handlers[event] = fn},
    window: {addEventListener: (event, fn) => handlers[event] = fn},
    localStorage: {getItem: () => {if (blocked) throw Error('Denied'); return saved;}, setItem: (key, value) => {if (blocked) throw Error('Denied'); writes.push([key, value]);}}
  };
  vm.runInNewContext(script, context);
  assert.equal(root.dataset.appearance, ['light', 'dark'].includes(saved) ? saved : 'system');
  handlers.DOMContentLoaded();
  return {handlers, root, control, writes};
}
for (const saved of [null, 'invalid', 'light', 'dark', 'system']) {
  const x = start(saved);
  assert.equal(x.control.value, x.root.dataset.appearance);
  x.control.value = 'dark'; x.handlers.change();
  assert.equal(x.root.dataset.appearance, 'dark');
  assert.deepEqual(x.writes, [['eutonos.appearance.v1', 'dark']]);
  x.handlers.storage({key: 'unrelated', newValue: 'light'});
  assert.equal(x.root.dataset.appearance, 'dark');
  x.handlers.storage({key: 'eutonos.appearance.v1', newValue: 'light'});
  assert.equal(x.control.value, 'light');
  x.handlers.storage({key: null, newValue: null});
  assert.equal(x.root.dataset.appearance, 'system');
}
const blocked = start(null, true);
blocked.control.value = 'light'; blocked.handlers.change();
assert.equal(blocked.root.dataset.appearance, 'light');
assert.equal(blocked.writes.length, 0);
console.log('Appearance: initialization, persistence, invalid values, cross-tab updates and blocked storage passed.');
