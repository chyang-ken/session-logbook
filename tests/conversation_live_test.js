// Exercise the shipped reader functions with controllable requests and timers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../index.html'), 'utf8');
const section = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
const context = vm.createContext({assert, console, URLSearchParams});
vm.runInContext(`
let selected = '', calls = [], renders = [], timers = new Map(), timerId = 0;
let state = {items: [{id:'a', starred:true}, {id:'b'}]}, _inflightMutations = 0;
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
vm.runInContext(section('function openConversation(', '// ---------- Recent files modal'), context);
vm.runInContext(section('let _convLiveId =', '// Tab refocus:'), context);
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
  console.log('PASS: overlay updates, scroll/metadata, selection, stale requests, close, search, hidden, retry, standalone');
})().catch(error=>{console.error(error);process.exitCode=1;});
