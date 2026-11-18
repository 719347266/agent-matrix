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
const element = {innerHTML: "", textContent: "", className: "", classList: {add(){},remove(){},toggle(){}}, value:""};
const context = vm.createContext({ location:{hash:""},
  console, Date, document: {addEventListener(){},querySelector: () => element, querySelectorAll: () => [], activeElement: null, documentElement: {dataset: {}}},
  setTimeout: () => 1, clearTimeout(){}, setInterval: () => 1, clearInterval(){},
  matchMedia: () => ({matches: false, addEventListener(){}}),
  window: {scrollTo(){}, addEventListener(){}, matchMedia: () => ({matches: false, addEventListener(){}})},
});
vm.runInContext(source.replace("/*__DATA__*/null", JSON.stringify(fixture)), context);
const evaluate = s => vm.runInContext(s, context);
const plain = s => JSON.parse(JSON.stringify(evaluate(s)));
function reset(item) {
  evaluate(`D.items = [${JSON.stringify(item)}]; D.threads = {}; D.judgments = {}; S.working = false; S.view = "todo"; S.stage = null;`);
}
(async () => {
  reset({n:45,title:"测试事项",status:"待确认",who:"AI"});
  assert.deepEqual(plain("detailButtons(byN(45)).map(b=>b[1])"), ["me","ai","date","done","cancel"]);
  assert.match(evaluate("itemView(byN(45))"), /已经办完/);
  evaluate('byN(45).status="进行中"; S.working=true');
  assert.deepEqual(plain("detailButtons(byN(45)).map(b=>b[1])"), ["cancel","done"]);
  await evaluate('apply(45,"cancel",null)');
  assert.equal(evaluate("byN(45).status"), "取消");
  assert.equal(evaluate("isWorking(byN(45))"), false);
  assert.deepEqual(plain("statusOf(byN(45))"), ["不做",""]);
  assert.deepEqual(plain("detailButtons(byN(45)).map(b=>b[1])"), ["restore"]);
  evaluate('D.judgments[45]={decision:"skip"}');
  await evaluate('apply(45,"restore",null)');
  assert.equal(evaluate("byN(45).status"), "待确认");
  assert.equal(evaluate("D.judgments[45]"), undefined);
  await evaluate('apply(45,"done",null)');
  evaluate('S.item=45; document.querySelector("#saybox").value="补充留档"; sendNote(false)');
  assert.equal(evaluate("byN(45).status"), "完成");
  assert.equal(evaluate('D.threads[45].at(-1).text'), "补充留档");
  evaluate('S.working=true'); // a late job snapshot cannot resurrect a closed item
  assert.equal(evaluate("isWorking(byN(45))"), false);
  assert.match(evaluate("statusOf(byN(45))[0]"),/已完成/);
  assert.match(evaluate("sysPage()"),/Slack/);
  for (const label of ["Notion","只存本机","本机 AI","整理"]) assert.ok(evaluate("sysPage()").includes(label));
  evaluate('S.item=45; byN(45).status="进行中"; document.querySelector("#saybox").value="电话确认了"; sendNote(true)');
  assert.equal(evaluate("byN(45).closed.by"),"你");
  assert.match(evaluate("thread(byN(45))"),/电话确认了/);
  assert.match(evaluate("closedText(byN(45))"),/你标记完成/);
  console.log("Client checks passed: new, running, cancel, restore, complete, closed-item conversation, stale status, concurrency copy.");
})().catch(e => { console.error(e); process.exitCode=1; });
