// Run with: node tests/test_client.cjs (no browser or npm dependencies).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname, "../src/agent_matrix/app/xinxiang.html"), "utf8");
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const fixture = {
  today: "2026-11-17", demo: true, generated_at: "10:00", settings: {}, judgments: {},
  items: [], threads: {}, deliverables: {}, inbox: [], worker: {}, system: {}
};
const element = {innerHTML: "", textContent: "", className: "", classList: {add(){},remove(){}}, value:""};
const context = vm.createContext({
  console, Date, document: {querySelector: () => element, querySelectorAll: () => [], activeElement: null},
  setTimeout: () => 1, clearTimeout(){}, setInterval: () => 1, clearInterval(){},
  window: {scrollTo(){}},
});
vm.runInContext(source.replace("/*__DATA__*/null", JSON.stringify(fixture)), context);
const evaluate = s => vm.runInContext(s, context);
const plain = s => JSON.parse(JSON.stringify(evaluate(s)));
function reset(item) {
  evaluate(`D.items = [${JSON.stringify(item)}]; D.threads = {}; D.judgments = {}; S.working = false; S.view = "home"; S.stage = null;`);
}
(async () => {
  reset({n:45,title:"测试事项",status:"待确认",who:"AI"});
  assert.deepEqual(plain("mainButtons(byN(45)).map(b=>b[1])"), ["me","ai","cancel","done"]);
  assert.match(evaluate("nowBox(byN(45))"), /已完成/);
  evaluate('byN(45).status="进行中"; S.working=true');
  assert.deepEqual(plain("mainButtons(byN(45)).map(b=>b[1])"), ["cancel","done"]);
  await evaluate('apply(45,"cancel",null)');
  assert.equal(evaluate("byN(45).status"), "取消");
  assert.equal(evaluate("isWorking(byN(45))"), false);
  assert.deepEqual(plain("stateTag(byN(45))"), ["不做",""]);
  assert.deepEqual(plain("mainButtons(byN(45)).map(b=>b[1])"), ["restore"]);
  evaluate('D.judgments[45]={decision:"skip"}');
  await evaluate('apply(45,"restore",null)');
  assert.equal(evaluate("byN(45).status"), "待确认");
  assert.equal(evaluate("D.judgments[45]"), undefined);
  await evaluate('apply(45,"done",null)');
  await evaluate('apply(45,"add","补充留档")');
  assert.equal(evaluate("byN(45).status"), "完成");
  assert.equal(evaluate('D.threads[45].at(-1).text'), "补充：补充留档");
  evaluate('S.working=true'); // a late job snapshot cannot resurrect a closed item
  assert.equal(evaluate("isWorking(byN(45))"), false);
  assert.equal(evaluate('stages().find(s=>s.key==="doing").items.length'), 0);
  assert.match(evaluate('stageList({key:"doing",items:[]})'), /最多同时做 10 件/);
  console.log("Client checks passed: new, running, cancel, restore, complete, closed-item conversation, stale status, concurrency copy.");
})().catch(e => { console.error(e); process.exitCode=1; });
