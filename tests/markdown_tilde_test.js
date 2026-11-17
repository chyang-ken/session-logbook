// Run the shipped markdown setup from index.html against the vendored marked build.
// A lone "~" means "about" in agent replies; only "~~text~~" may become strikethrough.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const from = html.indexOf('function strikethroughNeedsDoubleTilde(');
const to = html.indexOf('function markdownPreviewText(', from);
assert.ok(from !== -1 && to !== -1, 'markdown setup section not found');

const context = vm.createContext({
  DOMPurify: {sanitize: s => s},
  escapeHtml: s => s,
});
vm.runInContext(fs.readFileSync(path.join(root, 'vendor/marked.umd.js'), 'utf8'), context);
assert.equal(typeof context.marked, 'object', 'vendored marked did not load');

// The vendored build strikes a single-tilde pair through on its own; if that ever
// changes, the override below is no longer what this test is exercising.
assert.match(context.marked.parse('3~5 days and 10~20 files'), /<del>/,
  'vendored marked no longer treats ~x~ as strikethrough; revisit this test');

vm.runInContext(html.slice(from, to), context);
const render = s => context.renderMarkdown(s);

const cases = [
  '3~5 days and 10~20 files',
  'range 1~2, 3~4',
  '~5 min~',
  '~5~10',
  'path ~/foo and ~/bar',
];
for (const text of cases) {
  const out = render(text);
  assert.doesNotMatch(out, /<del>/, `single tilde struck through: ${text} -> ${out}`);
  assert.equal((out.match(/~/g) || []).length, (text.match(/~/g) || []).length,
    `a tilde was swallowed: ${text} -> ${out}`);
}

assert.match(render('this is ~~gone~~ now'), /<del>gone<\/del>/);
assert.match(render('1~2 and ~~gone~~'), /1~2 and <del>gone<\/del>/);
assert.match(render('```\n~a~\n```'), /<code>~a~\n<\/code>/);
console.log('markdown tilde: ok');
