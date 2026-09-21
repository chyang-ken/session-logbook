// Exercise the shipped reader functions with controllable requests and timers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../index.html'), 'utf8');
const section = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
const context = vm.createContext({assert, console, URLSearchParams});
vm.runInContext(`
let selected = '', calls = [], renders = [], timers = new Map(), timerId = 0;
let state = {items: []}, _inflightMutations = 0;
let toasts = [];
const toast = (msg, opts) => toasts.push({msg, opts});
let _convStandalone = false, _convNav = null, _lastInteractionAt = 0;
const ICONS = {close:''};
const classes = new Set();
let body = {scrollTop:37, scrollHeight:1000, clientHeight:200, querySelectorAll:()=>[]};
let findInput = {value:''};
const $conv = {
  classList: {add:x=>classes.add(x), remove:x=>classes.delete(x), contains:x=>classes.has(x)},
  querySelector: sel => sel === '.conv-body' ? body : sel === '#conv-find' ? findInput : {addEventListener(){}},
  innerHTML: ''
};
const document = {visibilityState:'visible'};
const location = {href:'/'};
const window = {getSelection:()=>({toString:()=>selected})};
const ensureConvOverlay = ()=>{};
const escapeHtml = x=>x;
const liveInterval = ()=>3000;
const setTimeout = fn=>{timers.set(++timerId,fn);return timerId;};
const clearTimeout = id=>timers.delete(id);
const fetch = url=>new Promise((resolve,reject)=>calls.push({url,resolve,reject}));
const renderConv = (data,meta)=>{renders.push({data,meta});body={...body,scrollTop:0};};
const reply = (n,data)=>calls[n].resolve({ok:true,json:async()=>data});
`, context);
vm.runInContext(section('// ---------- Conversation identity ----------', '// Column width bounds'), context);
vm.runInContext(section('function openConversation(', '// ---------- Recent files modal'), context);
vm.runInContext(section('let _convLiveId =', '// Tab refocus:'), context);
vm.runInContext(`setItems([{id:'a', starred:true}, {id:'b'}]);`, context);
const run = s=>vm.runInContext(s,context);
const flush = async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
(async()=>{
  run("openConversation('a'); reply(0,{id:'a',fingerprint:'1'});"); await flush();
  run("assert.equal(_convLiveId,'a'); assert.equal(timers.size,1); assert.equal(_convStandalone,false); body.scrollTop=37;");
  let poll=run('convLivePoll()');
  run("assert.match(calls[1].url,/fingerprint=1/); reply(1,{id:'a',fingerprint:'2'});"); await poll;
  run("assert.equal(renders.length,2); assert.equal(body.scrollTop,37); assert.equal(renders[1].meta.starred,true);");
  poll=run('convLivePoll()');
  run("selected='copying'; reply(2,{id:'a',fingerprint:'3'});"); await poll;
  run("assert.equal(renders.length,2); assert.equal(_convLiveFingerprint,'2'); selected='';");
  poll=run('convLivePoll()');
  run("openConversation('b'); reply(4,{id:'b',fingerprint:'b1'});"); await flush();
  run("reply(3,{id:'a',fingerprint:'stale'});"); await poll;
  run("assert.equal(renders.at(-1).data.id,'b'); assert.equal(_convLiveFingerprint,'b1');");
  poll=run('convLivePoll()');
  run("closeConversation(); reply(5,{id:'b',fingerprint:'late'});"); await poll;
  run("assert.equal(_convLiveId,null); assert.equal(timers.size,0); assert.equal(renders.at(-1).data.fingerprint,'b1');");
  run("openConversation('a'); closeConversation(); reply(6,{id:'a',fingerprint:'late-initial'});"); await flush();
  run("assert.equal(_convLiveId,null); assert.equal(timers.size,0);");
  run("openConversation('a'); openConversation('b'); reply(8,{id:'b',fingerprint:'b2'});"); await flush();
  run("reply(7,{id:'a',fingerprint:'late-initial'});"); await flush();
  run("assert.equal(renders.at(-1).data.id,'b');");
  run("findInput.value='search';"); await run('convLivePoll()');
  run("assert.equal(calls.length,9); findInput.value=''; document.visibilityState='hidden';");
  await run('convLivePoll()');
  run("assert.equal(calls.length,9); document.visibilityState='visible';");
  poll=run('convLivePoll()'); run("calls[9].reject(new Error('offline'));"); await poll;
  run("assert.equal(_convLivePolling,false); assert.equal(timers.size,1);");
  run("closeConversation(); openConversation('a',{standalone:true}); reply(10,{id:'a',fingerprint:'s1'});"); await flush();
  run("assert.equal(_convLiveId,'a'); assert.equal(timers.size,1); closeConversation(); assert.equal(_convLiveId,'a'); stopConvLive();");
  run("openConversation('a',{standalone:true,includeRewound:true}); assert.match(calls[11].url,/include_rewound=1/); reply(11,{id:'a',fingerprint:'h1'});"); await flush();
  poll=run('convLivePoll()'); run("assert.match(calls[12].url,/include_rewound=1/); reply(12,{id:'a',unchanged:true,fingerprint:'h1'});"); await poll;
  run("stopConvLive();");

  // A rewind while the page is open: the record being followed stops being the current one.
  // The reader must say so and offer the new record, and must not navigate on its own.
  run("toasts=[]; openConversation('a'); reply(13,{id:'a',fingerprint:'c1',conversation_current_id:'a',conversation_records:[{id:'a',relation:null,is_current:true}]});");
  await flush();
  run("assert.equal(_convLiveWasCurrent,true); assert.equal(toasts.length,0);");
  poll=run('convLivePoll()');
  run("reply(14,{id:'a',fingerprint:'c2',conversation_current_id:'a2',conversation_records:[{id:'a',relation:null,is_current:false},{id:'a2',relation:'rewind',is_current:true}]});");
  await poll;
  run("assert.equal(toasts.length,1,'the move is announced once');");
  run("assert.match(toasts[0].msg,/continued in a new record/);");
  run("assert.equal(toasts[0].opts.action.label,'Open it');");
  run("assert.equal(_convLiveId,'a','the reader stays on the record the URL named');");
  run("assert.equal(_convLiveWasCurrent,false);");
  poll=run('convLivePoll()');
  run("reply(15,{id:'a',fingerprint:'c3',conversation_current_id:'a2',conversation_records:[{id:'a',relation:null,is_current:false},{id:'a2',relation:'rewind',is_current:true}]});");
  await poll;
  run("assert.equal(toasts.length,1,'and not announced again on every later poll');");
  run("stopConvLive(); assert.equal(_convLiveWasCurrent,false);");

  console.log('PASS: overlay updates, scroll/metadata, selection, stale requests, close, search, hidden, retry, standalone, conversation moved');
})().catch(error=>{console.error(error);process.exitCode=1;});

// Execute the real renderer in both entry modes. Only DOM plumbing is stubbed;
// replacing renderConv itself would miss a feature gated to one mode.
const renderContext = vm.createContext({assert, console});
vm.runInContext(`
function element() {
  return {innerHTML:'', scrollTop:0, addEventListener(){}, setAttribute(){},
    querySelectorAll(){return [];},
    querySelector(selector) {
      return selector.startsWith('#') && this.innerHTML.includes('id="'+selector.slice(1)+'"')
        ? element() : null;
    }};
}
const head=element(), body=element();
const $conv={setAttribute(){},querySelector:s=>s==='.conv-head'?head:body};
const ICONS={};
const escapeHtml=x=>String(x ?? '');
const fmtAbsTime=x=>String(x);
const activityISO=x=>x.activity_at_iso || '';
const bindConvNav=()=>null, bindConvFind=()=>{};
const closeConversation=()=>{};
const document={title:''};
const state={items:[]};
let _convStandalone=false, _convRaw=false, $convNav=null;
`, renderContext);
vm.runInContext(section('// ---------- Conversation identity ----------', '// Column width bounds'), renderContext);
vm.runInContext(section('function renderConv(', '// User-msg navigation:'), renderContext);
// The turn renderer lives further down the file; load it too, so the event rows this
// harness asserts on are produced by the real code rather than a stub.
vm.runInContext(section('const FOLD_LIMITS = {', 'function renderQaQuestion('), renderContext);
vm.runInContext(`const renderConvTool = t => '<div class="conv-tool"></div>';
const renderMarkdown = x => String(x ?? '');
const renderFoldableText = (text, limit) => String(text ?? '');`, renderContext);
vm.runInContext(`
const chain=[{id:'old',relation:null,is_current:false},
             {id:'current',relation:'rewind',is_current:true}];
const base={id:'current',source:'claude',turns:[],total_lines:1};
// Every case is rendered twice: once as the modal (a card exists) and once as the full page
// (no card at all). A feature that only works in one of the two is the exact bug this catches.
const cases=[
  // The current record of a multi-record conversation lists its records and links each one.
  [{conversation_id:'conv',conversation_current_id:'current',conversation_records:chain},
   ['Records in this conversation (2)','Rewound from the previous record','/?session=old',
    'reading this one']],
  // An earlier record opens as itself and offers the current one; it never redirects.
  [{id:'old',conversation_id:'conv',conversation_current_id:'current',conversation_records:chain},
   ['This is an earlier record','Open the current one','/?session=current','First record']],
  // A fork is a separate conversation that only reports where it came from.
  [{forked_from_record_id:'source-rec'},['Forked from','/?session=source-rec','separate conversation']],
  // A Codex sub-agent names the session that started it, as lineage and not as a warning:
  // its own file is its whole record, so nothing about it is incomplete.
  [{spawned_from:{parent_session_id:'parent-sess',root_session_id:'parent-sess'}},
   ['Sub-agent session started by','/?session=parent-sess','this is its full transcript']],
  // Codex did not always record the spawner; the relationship is still stated plainly.
  [{spawned_from:{parent_session_id:null,root_session_id:null}},
   ['Sub-agent session started by','a session Codex did not record']],
  // The in-file rewind axis is untouched by conversation identity.
  [{rewind:{status:'selected',hidden_message_count:2}},
   ['View earlier saved records','include_rewound=1']],
  [{include_rewound:true},['All saved records','Back to current conversation']],
  // A personal title inherited from a superseded record says where it came from.
  [{conversation_id:'conv',conversation_current_id:'current',conversation_records:chain,
    title_override:'My name for it',title_override_source_id:'old'},
   ['Title set on an earlier record']],
  // Rows an in-file rewind abandoned are counted out loud, never silently missing.
  [{rewind_abandoned_rows:3},['3 records were abandoned by a rewind and are not shown.']],
  [{rewind_abandoned_rows:1},['1 record was abandoned by a rewind and is not shown.']],
  // The conversation's note is readable and editable from the reader itself, not only the
  // card -- and the card does not exist at all on the full page.
  [{note:'Rerun this against the staging copy.'},
   ['conv-note-text','Rerun this against the staging copy.','id="conv-note-edit"','Edit note']],
  // With no note, the reader offers to add one and shows no empty note block.
  [{},['id="conv-note-edit"','Add note']],
  // Notes left on superseded records are listed beside the current one, each labelled with
  // the record it belongs to, and never merged into it.
  [{conversation_id:'conv',conversation_current_id:'current',conversation_records:chain,
    note:'Current thinking',older_notes:[{record_id:'old',note:'Pre-rewind thought'}]},
   ['conv-older-notes','conv-older-note-id','Pre-rewind thought','Current thinking']],
  // An earlier record deep-linked on its own still shows the conversation's note.
  [{id:'old',conversation_id:'conv',conversation_current_id:'current',
    conversation_records:chain,note:'Lives on the current record'},
   ['Lives on the current record','id="conv-note-edit"']],
];
for (const [fields,expected] of cases) {
  for (const standalone of [false,true]) {
    _convStandalone=standalone;
    const recordId=fields.id||base.id;
    setItems(standalone?[]:[{id:recordId,size:100,...fields}]);
    renderConv({...base,...fields},standalone?null:findItem(recordId));
    for(const text of expected) assert.ok(head.innerHTML.includes(text),
      (standalone?'full page':'modal')+' missing '+text);
  }
}
// An ordinary single-record session draws no conversation chrome at all.
for (const standalone of [false,true]) {
  _convStandalone=standalone;
  setItems(standalone?[]:[{id:'current',size:100}]);
  renderConv({...base},standalone?null:findItem('current'));
  for (const text of ['Records in this conversation','This is an earlier record','Forked from',
                      'Sub-agent session started by','abandoned by a rewind']) {
    assert.ok(!head.innerHTML.includes(text), (standalone?'full page':'modal')+' gained '+text);
  }
}
// A zero count, or the field a backend without it never sends, must render nothing.
for (const fields of [{rewind_abandoned_rows:0},{rewind_abandoned_rows:null},{}]) {
  renderConv({...base,...fields},null);
  assert.ok(!head.innerHTML.includes('abandoned by a rewind'),
    'a missing or zero abandoned-row count must stay silent');
}
// With nothing written, the reader shows the affordance and no note block at all.
for (const standalone of [false,true]) {
  _convStandalone=standalone;
  setItems(standalone?[]:[{id:'current',size:100}]);
  renderConv({...base},standalone?null:findItem('current'));
  assert.ok(!head.innerHTML.includes('conv-note-text'),
    (standalone?'full page':'modal')+' drew an empty note block');
  assert.ok(!head.innerHTML.includes('conv-older-notes'),
    (standalone?'full page':'modal')+' drew an empty earlier-notes block');
  assert.ok(head.innerHTML.includes('Add note'),
    (standalone?'full page':'modal')+' hid the add-note affordance');
}

// The three Claude event kinds are rendered in place, as system events, in both modes.
// The bar they must clear: never the user class, because the whole point is that none of
// them is something the person said and had received.
const eventTurns=[
  {type:'user',text:'Real human turn',ts:'t0'},
  {type:'queued_input',text:'Typed while it was busy',ts:'t1'},
  {type:'system_notification',text:'[completed] Agent "Sweep" finished',delivered:false,ts:'t2'},
  {type:'system_notification',text:'[completed] Agent "Other" finished',delivered:true,ts:'t3'},
  {type:'api_error',text:'529 Overloaded \\u2014 retried 4 times over 3m00s',retries:4,ts:'t4'},
];
for (const standalone of [false,true]) {
  _convStandalone=standalone;
  setItems(standalone?[]:[{id:'current',size:100}]);
  renderConv({...base,turns:eventTurns},standalone?null:findItem('current'));
  const where=standalone?'full page':'modal';
  for (const text of ['conv-queued-input','Queued input','delivery not confirmed',
                      'Typed while it was busy','conv-api-error','retried 4 times over 3m00s',
                      'Agent "Sweep" finished','Agent "Other" finished']) {
    assert.ok(body.innerHTML.includes(text), where+' missing '+text);
  }
  // Each event row carries the shared system-event class and none carries the user class.
  // The trailing space matters: conv-turn-head would otherwise match as a row of its own.
  const rows=body.innerHTML.split('<div class="conv-turn ').slice(1);
  assert.equal(rows.length,5,where+' turn count');
  for (const row of rows.slice(1)) {
    assert.ok(row.includes('conv-system-event'),where+' event row lost its system class');
    assert.ok(!row.includes('conv-user'),where+' event row was styled as a user message');
  }
  assert.ok(rows[0].includes('conv-user'),where+' lost the real user turn');
  // One "delivery not confirmed" mark: the queued input's, plus the queued notification's.
  assert.equal(body.innerHTML.split('delivery not confirmed').length-1,2,
    where+' mislabelled a delivered record as unconfirmed');
  // The reader's own message counter, the j/k range and the goto ceiling all read "1".
  assert.ok(head.innerHTML.includes('1 msgs'),where+' counted an event as a message');
  assert.ok(head.innerHTML.includes('max="1"'),where+' let goto reach an event row');
}
console.log('PASS: real renderer exposes conversation navigation in modal and full page');
`, renderContext);
