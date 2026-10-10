// Exercise the shipped paged search loop from index.html, without a browser.
// A search that arrives in many pages must not redraw the whole list for every page:
// redrawing hundreds of cards froze the page for up to a second each time.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../index.html'), 'utf8');
const from = html.indexOf('function applySearchHits(hits');
const to = html.indexOf('async function load()', from);
assert.ok(from !== -1 && to !== -1, 'paged search section not found');

const pages = [];
let renders = 0;
const hit = id => ({id, snippets: [{text: id, role: 'user', term: 'x'}]});
const context = vm.createContext({
  assert, console,
  state: {searchHits: {}, searchPending: false, searchHasPage: false},
  $count: {textContent: ''},
  $searchWrap: {classList: {toggle() {}, add() {}, remove() {}}},
  $list: {cards: 0, querySelector(sel) { return sel === '.card' && this.cards ? {} : null; }},
  render() {
    renders += 1;
    context.$list.cards = Object.keys(context.state.searchHits).length;
    context.$count.textContent = context.$list.cards + ' / 100 sessions'
      + (context.state.searchPending ? ' · searching older…' : '');
  },
  fetch: async url => ({ok: true, json: async () => pages.shift()}),
  displayedItems: () => ({filtered: Object.keys(context.state.searchHits)}),
  setTimeout: (fn) => setImmediate(fn),
  sessionsSignature: items => items.map(m => m.id).join(','),
  setItems(items) { context.state.items = items; },
  _liveSig: 'a',
});
vm.runInContext(html.slice(from, to), context);

(async () => {
  // Five pages, each with hits: the list is drawn for the first page and the last only.
  pages.push(...[1, 2, 3, 4, 5].map(n => ({hits: [hit('s' + n)], next: n < 5 ? 'c' + n : null})));
  const counts = [];
  const origFetch = context.fetch;
  context.fetch = async url => { counts.push(context.$count.textContent); return origFetch(url); };
  await vm.runInContext('runPagedSearch("x", () => false)', context);
  assert.equal(renders, 2, 'one redraw for the newest page and one when the search ends');
  assert.equal(Object.keys(context.state.searchHits).length, 5, 'every page is kept');
  assert.equal(context.state.searchPending, false);
  assert.ok(counts.some(t => t.includes('found so far, searching older…')), counts.join('\n'));

  // While nothing is on screen yet (an empty first page), each page redraws until a match shows.
  renders = 0; context.$list.cards = 0; context.state.searchHits = {};
  pages.push({hits: [], next: 'a'}, {hits: [], next: 'b'}, {hits: [hit('late')], next: 'c'},
             {hits: [hit('later')], next: null});
  await vm.runInContext('runPagedSearch("x", () => false)', context);
  assert.equal(renders, 4, 'empty pages redraw so the first match appears at once; then the end');

  // A newer search takes over: the old one stops without drawing.
  renders = 0;
  pages.push({hits: [hit('old')], next: 'z'});
  await vm.runInContext('runPagedSearch("x", () => true)', context);
  assert.equal(renders, 0);
  assert.equal(pages.length, 1, 'a stale search does not even send its request');
  pages.length = 0;

  // A page that fails once is tried again; the search goes on as if nothing happened.
  renders = 0; context.state.searchHits = {};
  let failures = 1;
  context.fetch = async url => {
    if (failures-- > 0) throw new TypeError('Failed to fetch');
    return {ok: true, json: async () => pages.shift()};
  };
  pages.push({hits: [hit('r1')], next: null});
  assert.equal(await vm.runInContext('runPagedSearch("x", () => false)', context), true);
  assert.equal(context.state.searchError, null);
  assert.deepEqual(Object.keys(context.state.searchHits), ['r1']);

  // A first page that fails twice: nothing to show, and the search says it failed.
  context.fetch = async () => ({ok: false, status: 500, json: async () => ({})});
  context.state.searchHits = {stale: []};
  assert.equal(await vm.runInContext('runPagedSearch("x", () => false)', context), false);
  assert.match(context.state.searchError, /500/);
  assert.equal(Object.keys(context.state.searchHits).length, 0, 'a failed first page leaves no stale matches');
  assert.equal(context.state.searchPending, false);

  // A later page that fails twice keeps the matches already drawn, and still says it failed.
  const later = [{hits: [hit('p1')], next: 'c'}];
  context.fetch = async () => later.length
    ? {ok: true, json: async () => later.shift()}
    : {ok: false, status: 503, json: async () => ({})};
  assert.equal(await vm.runInContext('runPagedSearch("x", () => false)', context), false);
  assert.deepEqual(Object.keys(context.state.searchHits), ['p1']);
  assert.match(context.state.searchError, /503/);

  // Starting a search clears the list before any request goes out, and does not wait for
  // the session list: the first request is the search itself.
  const urls = [];
  let shownAtFirstRequest = null;
  context.state.items = [{id: 'a'}];
  context.state.searchHits = {old: []};
  context.fetch = async url => {
    if (shownAtFirstRequest === null) shownAtFirstRequest = Object.keys(context.state.searchHits).length;
    urls.push(url);
    if (url.startsWith('/api/sessions')) return {ok: true, json: async () => [{id: 'a'}, {id: 'b'}]};
    return {ok: true, json: async () => (urls.length === 1
      ? {hits: [hit('a')], next: null}
      : {hits: [hit('a'), hit('b')], next: null})};
  };
  renders = 0;
  await vm.runInContext('startSearch("x")', context);
  assert.equal(shownAtFirstRequest, 0, 'the old matches were cleared before searching');
  assert.ok(urls[0].startsWith('/api/search?'), urls.join('\n'));
  // Then the list refresh found a new session, so the newest page ran again and only the
  // new match was added (no duplicate snippet for "a").
  assert.ok(urls[1].startsWith('/api/sessions'), urls.join('\n'));
  assert.ok(urls[2].startsWith('/api/search?'), urls.join('\n'));
  assert.deepEqual(Object.keys(context.state.searchHits).sort(), ['a', 'b']);
  assert.equal(context.state.searchHits.a.length, 1);

  // With no new session the refresh adds no request and no redraw.
  urls.length = 0; context._liveSig = 'a,b'; context.state.items = [{id: 'a'}, {id: 'b'}];
  renders = 0;
  await vm.runInContext('startSearch("x")', context);
  assert.equal(urls.length, 2, urls.join('\n'));
  assert.equal(renders, 2, 'one clear, one result draw');

  console.log('search paging frontend: all assertions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
