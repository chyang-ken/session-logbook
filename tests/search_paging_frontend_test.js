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

  console.log('search paging frontend: all assertions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
