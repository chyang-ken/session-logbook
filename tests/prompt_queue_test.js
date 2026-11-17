'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const queue = html.slice(html.lastIndexOf('// ---------- Prompt Queue ----------'));
function extract(name) {
  const start = queue.indexOf('  function ' + name + '(');
  assert(start >= 0, name);
  const end = queue.indexOf('\n  }', start);
  return queue.slice(start, end + 4);
}
const storage = new Map();
const context = vm.createContext({
  localStorage: {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
  },
  toast() {},
  updateSelection() {},
  copyText(text) { context.copied = text; },
});
vm.runInContext(`const STORAGE_KEY = 'qq.items'; const FOLDS_KEY = 'qq.folds';
let items = []; let selectionStart = null; let selecting = true; const selected = new Set();`, context);
for (const name of ['textListSignature', 'loadFoldedPositions', 'loadItems', 'saveItems', 'selectItem', 'copySelected']) {
  vm.runInContext(extract(name), context);
}
const run = code => vm.runInContext(code, context);
const plain = code => JSON.parse(JSON.stringify(run(code)));
storage.set('qq.items', JSON.stringify(['First', 'Second\nLine', 'Third', '   ']));
run('items = loadItems()');
assert.deepEqual(plain('items.map(item => item.collapsed)'), [false, false, false, false]);
run('items[1].collapsed = true; saveItems()');
assert.deepEqual(JSON.parse(storage.get('qq.items')), ['First', 'Second\nLine', 'Third', '   ']);
run('items = loadItems()');
assert.equal(run('items[1].collapsed'), true);
// An old dashboard can edit the string array without applying stale fold positions.
storage.set('qq.items', JSON.stringify(['Edited', 'First', 'Second\nLine', 'Third']));
run('items = loadItems()');
assert.equal(run('items.some(item => item.collapsed)'), false);
run('selectItem(items[3], true, false); selectItem(items[1], true, true); copySelected()');
assert.equal(context.copied, '<ref1>\nFirst\n</ref1>\n\n<ref2>\nSecond\nLine\n</ref2>\n\n<ref3>\nThird\n</ref3>');
run('selectItem(items[2], false, true)');
assert.deepEqual(plain('items.map(item => selected.has(item))'), [false, true, false, false]);
run('selecting = false');
context.copied = null;
run('copySelected()');
assert.equal(context.copied, null);
// A fold-metadata quota failure must not discard successfully saved text.
context.localStorage.setItem = (key, value) => {
  if (key === 'qq.folds') throw Error('Synthetic quota failure');
  storage.set(key, value);
};
assert.equal(run('saveItems()'), true);
assert.equal(JSON.parse(storage.get('qq.items'))[0], 'Edited');
console.log('Prompt Queue storage compatibility, selection, and copy passed');
