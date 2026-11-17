// Exercise the shipped timeline batching from index.html, without a browser.
// Drawing every card at once froze the page for seconds; the timeline draws the newest
// batch first and the next batch only when the end of what is drawn nears the screen.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../index.html'), 'utf8');
const from = html.indexOf('// ---------- Timeline drawn in batches');
const to = html.indexOf('// ---------- Global collapse / expand', from);
assert.ok(from !== -1 && to !== -1, 'timeline batch section not found');

// A fake list: cards are counted by a marker renderCard writes; "more" is the footer.
const fake = {cards: [], more: null, moreTop: 0, bound: 0};
const list = {
  get children() { return fake.cards; },
  insertAdjacentHTML(where, markup) {
    if (where === 'beforeend') fake.cards.push(...(markup.match(/<c [^>]*>/g) || []));
    else if (where === 'afterend') fake.more = markup.includes('timeline-more') ? markup : null;
  },
};
const $list = {
  scrollTop: 0,
  set innerHTML(markup) {
    fake.cards = markup.match(/<c [^>]*>/g) || [];
    fake.more = markup.includes('timeline-more') ? markup : null;
  },
  querySelector: sel => (sel === '.timeline-list' ? list : null),
  addEventListener() {},
};
const context = vm.createContext({
  assert, $list,
  state: {q: '', sourceFilter: 'all', hideOneshot: true},
  window: {innerHeight: 800, addEventListener() {}},
  document: {getElementById: id => (id === 'timeline-more' && fake.more
    ? {getBoundingClientRect: () => ({top: fake.moreTop}), remove() { fake.more = null; }}
    : null)},
  activityTime: m => m.t,
  renderCard: m => `<c ${m.id}>`,
  bindCardEvents(cards) { fake.bound += cards ? cards.length : fake.cards.length; },
  syncCollapseAllBtn() {},
});
vm.runInContext(html.slice(from, to), context);
const items = Array.from({length: 400}, (_, i) => ({id: 's' + i, t: i}));

// The footer is far below the screen: only the newest batch is drawn, newest first.
fake.moreTop = 100000;
context.drawTimeline(items);
assert.equal(fake.cards.length, 150);
assert.equal(fake.cards[0], '<c s399>');
assert.match(fake.more, /250 older sessions/);

// Scrolling near the footer draws the next batch and binds only the new cards.
fake.moreTop = 900; fake.bound = 0;
context.drawMoreTimelineIfNear();
assert.equal(fake.cards.length, 300);
assert.equal(fake.bound, 150);
assert.match(fake.more, /100 older sessions/);

// A redraw (live refresh) keeps what was drawn, so the scroll position holds.
fake.moreTop = 100000;
context.drawTimeline(items);
assert.equal(fake.cards.length, 300);

// The last batch removes the footer.
fake.moreTop = 0;
context.drawMoreTimelineIfNear();
assert.equal(fake.cards.length, 400);
assert.equal(fake.more, null);
context.drawMoreTimelineIfNear();
assert.equal(fake.cards.length, 400);

// A new search starts again from one batch.
context.state.q = 'widget'; fake.moreTop = 100000;
context.drawTimeline(items);
assert.equal(fake.cards.length, 150);

console.log('timeline batch frontend: all assertions passed');
