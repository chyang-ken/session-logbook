// Exercise the shipped conversation-identity helpers from index.html, without a browser.
// The sections below are read out of the real file, so these assertions test what ships.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../index.html'), 'utf8');
const section = (start, end) => {
  const from = html.indexOf(start);
  assert.notEqual(from, -1, `missing section start: ${start}`);
  const to = html.indexOf(end, from);
  assert.notEqual(to, -1, `missing section end: ${end}`);
  return html.slice(from, to);
};

const context = vm.createContext({assert, console});
vm.runInContext(`
const state = {
  items: [], searchHits: {}, q: '', sourceFilter: 'all', hideOneshot: false,
  viewMode: 'timeline', recentDays: 'all',
  cardCollapsed: new Set(), cardExpanded: new Set(),
};
const isSuspected = m => m.user_turn_count === 1 && !m.human_confirmed;
const activityTime = m => m.activity_at || m.mtime || 0;
const computeScope = m => m.archived ? 'archived' : m.starred ? 'starred' : 'recent';
const saveUIState = () => {};
`, context);

vm.runInContext(section('// ---------- Conversation identity ----------',
                        '// Column width bounds'), context);
vm.runInContext(section('function cardDefaultCollapsed(scope)',
                        'function renderDots('), context);
vm.runInContext(section('function displayedItems()', 'let _hasRendered'), context);
vm.runInContext(section('function applySearchHits(hits)', 'async function load()'), context);
vm.runInContext(section('function pruneCardCollapsed()', 'const _saved ='), context);

const run = s => vm.runInContext(s, context);

// A multi-record conversation (oldest to newest), a plain single-record session, and a fork.
run(`
const records = [
  {id: 'rec-old',  relation: null, is_current: false},
  {id: 'rec-mid',  relation: 'rewind', is_current: false},
  {id: 'rec-new',  relation: 'compaction-continuation', is_current: true},
];
const member = (id, extra) => Object.assign({
  id, source: 'claude', mtime: 10, project_path: '/p',
  conversation_id: 'conv-1', conversation_current_id: 'rec-new',
  conversation_records: records,
}, extra || {});
setItems([
  member('rec-old'),
  member('rec-mid'),
  member('rec-new', {starred: true, note: 'live note',
                     older_notes: [{record_id: 'rec-old', note: 'old note'}]}),
  {id: 'solo', source: 'codex', mtime: 5, conversation_id: 'solo',
   conversation_current_id: 'solo',
   conversation_records: [{id: 'solo', relation: null, is_current: true}]},
  {id: 'legacy', source: 'claude', mtime: 4},   // a backend that serves no conversation fields
]);
`);

// --- the resolver understands both kinds of id, record first ---
run(`assert.equal(findItem('rec-old').id, 'rec-old', 'a record id resolves to its own record');`);
run(`assert.equal(findItem('conv-1').id, 'rec-new', 'a conversation id resolves to the current record');`);
run(`assert.equal(currentItemFor('rec-old').id, 'rec-new', 'any member resolves to the current entry');`);
run(`assert.equal(currentItemFor('conv-1').id, 'rec-new');`);
run(`assert.equal(currentItemFor('solo').id, 'solo', 'a single-record session resolves to itself');`);
run(`assert.equal(currentItemFor('legacy').id, 'legacy', 'no conversation fields still resolves');`);
run(`assert.equal(findItem('nope'), undefined);`);
run(`assert.equal(findItem(''), undefined);`);

// A record id must never be overridden by a conversation id that happens to equal it.
run(`
setItems([{id: 'shared', conversation_id: 'shared', conversation_current_id: 'shared',
           conversation_records: [{id: 'shared', relation: null, is_current: true}]},
          {id: 'other', conversation_id: 'shared', conversation_current_id: 'other',
           conversation_records: [{id: 'shared', relation: null, is_current: false},
                                  {id: 'other', relation: 'rewind', is_current: true}]}]);
assert.equal(findItem('shared').id, 'shared', 'the record wins over a colliding conversation id');
`);

// --- one entry per conversation, and every count agrees with the list ---
run(`
setItems([
  member('rec-old'), member('rec-mid'), member('rec-new'),
  {id: 'solo', source: 'codex', mtime: 5},
  {id: 'legacy', source: 'claude', mtime: 4},
]);
const shown = displayedItems();
assert.deepEqual(shown.filtered.map(m => m.id).sort(), ['legacy', 'rec-new', 'solo']);
assert.equal(shown.scopedItems.length, 3, 'the denominator counts conversations too');
assert.equal(conversationItems().length, 3);
`);

// --- links: a conversation id only when the entry really has several records ---
run(`
assert.equal(shareId(findItem('rec-new')), 'conv-1', 'a multi-record entry links by conversation');
assert.equal(shareId(findItem('solo')), 'solo', 'a single-record entry keeps its record id');
assert.equal(shareId(findItem('legacy')), 'legacy', 'no conversation fields keeps the record id');
`);

// --- collapse state survives a rewind, and old record-keyed entries are still read ---
run(`
state.cardCollapsed = new Set(); state.cardExpanded = new Set();
setCardCollapsed(findItem('rec-new'), true);
assert.ok(state.cardCollapsed.has('conv-1'), 'the key written is the conversation');
assert.ok(!state.cardCollapsed.has('rec-new'), 'not the record that may be superseded next');
assert.ok(cardIsCollapsed(findItem('rec-new')));

state.cardCollapsed = new Set(['rec-new']);   // what an older build wrote
state.cardExpanded = new Set();
assert.ok(cardIsCollapsed(findItem('rec-new')), 'an old record-keyed override is still honoured');
setCardCollapsed(findItem('rec-new'), true);
assert.ok(!state.cardCollapsed.has('rec-new'), 'and is replaced, not duplicated');
assert.ok(state.cardCollapsed.has('conv-1'));

// Nothing here may throw on a shape an older build left behind.
state.cardCollapsed = new Set(); state.cardExpanded = new Set();
assert.equal(cardIsCollapsed({id: 'x'}), false, 'an item with no scope falls back to the Recent default');
assert.equal(cardIsCollapsed({id: 'x', scope: 'dusty'}), true);
assert.equal(cardKey({id: 'x'}), 'x');
`);

// --- pruning keeps both kinds of key alive ---
run(`
state.cardCollapsed = new Set(['conv-1', 'rec-old', 'gone']);
state.cardExpanded = new Set(['solo']);
pruneCardCollapsed();
assert.deepEqual([...state.cardCollapsed].sort(), ['conv-1', 'rec-old'],
  'a dead id goes; a live record key and a live conversation key both stay');
assert.deepEqual([...state.cardExpanded], ['solo']);
`);

// --- search folds a hit in a superseded record onto the conversation's entry ---
run(`
applySearchHits([
  {id: 'rec-old', conversation_id: 'conv-1', conversation_current_id: 'rec-new',
   snippets: [{text: 'only in the old record', role: 'user'}]},
  {id: 'rec-new', conversation_id: 'conv-1', conversation_current_id: 'rec-new',
   snippets: [{text: 'in the current record', role: 'user'}]},
  {id: 'solo', conversation_id: 'solo', conversation_current_id: 'solo',
   snippets: [{text: 'unrelated', role: 'user'}]},
]);
assert.deepEqual(Object.keys(state.searchHits).sort(), ['rec-new', 'solo']);
const folded = state.searchHits['rec-new'];
assert.equal(folded.length, 2, 'both records contribute their own snippets');
assert.equal(folded[0].from_older_record, true);
assert.equal(folded[0].record_id, 'rec-old', 'the snippet still names the file it came from');
assert.equal(folded[1].from_older_record, undefined);
assert.equal(state.searchHits['solo'][0].from_older_record, undefined);

// A backend that sends no conversation_current_id behaves exactly as before.
applySearchHits([{id: 'legacy', snippets: [{text: 'plain', role: 'user'}]}]);
assert.deepEqual(state.searchHits, {legacy: [{text: 'plain', role: 'user'}]});
applySearchHits(null);
assert.deepEqual(state.searchHits, {}, 'a failed search clears rather than throws');
`);

// --- the search filter and the fold agree: the entry is what gets matched ---
run(`
state.q = 'old';
applySearchHits([{id: 'rec-old', conversation_id: 'conv-1', conversation_current_id: 'rec-new',
                  snippets: [{text: 'only in the old record', role: 'user'}]}]);
assert.deepEqual(displayedItems().filtered.map(m => m.id), ['rec-new'],
  'a hit that only exists in an earlier record still surfaces its conversation');
state.q = '';
`);

// --- relation wording ---
run(`
assert.equal(recordRelationLabel(null), 'First record');
assert.equal(recordRelationLabel('rewind'), 'Rewound from the previous record');
assert.equal(recordRelationLabel('continuation'), 'Continued from the previous record');
assert.equal(recordRelationLabel('compaction-continuation'), 'Continued after a compaction');
assert.equal(recordRelationLabel('something-new'), 'something-new', 'an unknown relation is shown, not dropped');
`);

// --- editing the note from the reader writes once, and the card moves with it ---
// The reader and the card show the same note, so an edit made in one has to be visible in
// the other before the request lands, and has to come back out of both if it fails.
vm.runInContext(`
let posted = [], renders = 0, toasts = [], nextResponse = {ok: true};
const render = () => { renders += 1; };
const renderConv = () => {};
const $conv = {querySelector: () => null};
let _convStandalone = false;
const toast = message => toasts.push(message);
const mutatingFetch = async (url, options) => {
  posted.push({url, body: JSON.parse(options.body)});
  if (!nextResponse.ok) throw new Error('offline');
  return nextResponse;
};
const window = {openNoteModal: async () => window.__answer};
`, context);
vm.runInContext(section('function sessionMetadataTargets(data, meta)',
                        'async function setSessionHumanConfirmation('), context);

const finish = async body => {
  vm.runInContext(body, context);
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
};

(async () => {
  // A note typed in the reader lands on every view of the conversation, card included.
  run(`window.__answer = 'Rerun against staging';
       globalThis.pending = editSessionNote({id: 'rec-old'}, findItem('rec-old'));`);
  await finish(`;`);
  run(`
assert.equal(posted.length, 1, 'exactly one write');
assert.equal(posted[0].url, '/api/sessions/rec-old/note');
assert.deepEqual(posted[0].body, {note: 'Rerun against staging'});
assert.equal(currentItemFor('rec-old').note, 'Rerun against staging',
  'the card entry carries the optimistic value');
assert.ok(renders > 0, 'the list was redrawn so the card shows it');
assert.deepEqual(toasts, ['Note saved']);
`);

  // A failed write puts the old note back on the card, not just in the reader.
  run(`nextResponse = {ok: false}; posted = []; toasts = []; window.__answer = 'lost edit';
       globalThis.pending = editSessionNote({id: 'rec-new'}, findItem('rec-new'));`);
  await finish(`;`);
  run(`
assert.equal(currentItemFor('rec-new').note, 'Rerun against staging',
  'a failed write rolls the card back to the previous note');
assert.equal(toasts.length, 1);
assert.ok(toasts[0].startsWith('Note failed: '), toasts[0]);
`);

  // Cancelling the dialog writes nothing at all.
  run(`posted = []; toasts = []; window.__answer = null;
       globalThis.pending = editSessionNote({id: 'rec-new'}, findItem('rec-new'));`);
  await finish(`;`);
  run(`assert.deepEqual(posted, [], 'cancelling must not write');
       assert.deepEqual(toasts, []);`);

  console.log('conversation identity frontend: all assertions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
