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
  const settings = ["ai","src","data","general"].map(tab => evaluate(`S.setTab="${tab}"; sysPage()`)).join("");
  for (const label of ["Slack","Notion","只存本机","本机 AI","整理","新手引导"]) assert.ok(settings.includes(label), label);
  assert.ok(!evaluate('S.setTab="ai"; sysPage()').includes("Slack"), "one settings tab at a time");
  assert.ok(!/62 改|输入指令|编号 \+ 动作/.test(html), "no command syntax shown to the owner");
  for (let step = 0; step < evaluate("OB.length"); step++) {  // the first-run guide: every step renders its own title
    evaluate(`S.ob={step:${step}}`);
    assert.ok(evaluate("obView()").includes(evaluate(`OB[${step}].title`)));
  }
  assert.match(evaluate("S.ob={step:OB.length-1}; obView()"),/开始使用/);
  evaluate("S.ob=null");
  evaluate('S.item=45; byN(45).status="进行中"; document.querySelector("#saybox").value="电话确认了"; sendNote(true)');
  assert.equal(evaluate("byN(45).closed.by"),"你");
  assert.match(evaluate("thread(byN(45))"),/电话确认了/);
  assert.match(evaluate("closedText(byN(45))"),/你标记完成/);

  // A brief in the inbox opens in place as the whole report: groups, lines, each item's status NOW and its buttons.
  evaluate(`D.items=${JSON.stringify([
    {n:26,title:"核对清单",status:"待验收",who:"AI",url:"https://x.feishu.cn/docx/a"},
    {n:24,title:"定发布日期",status:"待确认",who:"我"},
    {n:9,title:"写周报",status:"进行中",who:"我",due:"2026-11-17"},
    {n:31,title:"算 ROI",status:"卡住",who:"AI",blocker:"缺收入导出"}])}; D.threads={}; D.deliverables={}; S.item=null; S.inline=null; S.openMsg=null;`);
  const brief={id:7,kind:"brief",title:"11/17 周二 · 早报",text:"1 场会 · 你要做 1 件",refs:[26],read:false,at:"2026-11-17T08:30:00",blocks:[
    {type:"text",text:"1 场会 · 你要做 1 件"},
    {type:"section",title:"今天的会 · 1 场",rows:[],more:"10:00  周会"},
    {type:"section",title:"今天你要做 · 1 件",rows:[{head:"#9 写周报",hint:"今天到期",url:null,actions:[],refs:[9]}],more:""},
    {type:"section",title:"AI 做好了，等你看 · 1 件",rows:[{head:"#26 核对清单",hint:"清单已整理",url:"https://x.feishu.cn/docx/a",actions:[],refs:[26]}],more:"还有 2 件，在看板里"},
    {type:"section",title:"需要你定 · 1 件",rows:[{head:"#24 定发布日期",hint:"还没定什么时候做完",url:null,actions:[],refs:[24]}],more:""},
    {type:"text",text:"[进入会议](https://vc.feishu.cn/j/1)"}]};
  // Older messages: a card that still told you what to reply, and a text brief.
  const oldCard={id:6,kind:"brief",title:"11/16 周一 · 早报",text:"",refs:[24,31],read:true,at:"2026-11-16T08:30:00",blocks:[
    {type:"section",title:"需要你定 · 1 件",rows:[{head:"#24 定发布日期",hint:"什么时候做完？回「24 周五」，不做回「24 不做」",url:null,actions:[],refs:[24]}],more:""},
    {type:"section",title:"AI 正在做 · 1 件",rows:[{head:"#31 算 ROI",hint:"AI 卡住：缺收入导出，补材料回「31 + 材料」",url:null,actions:[{label:"通过",cmd:"31 过"}],refs:[31]}],more:""},
    {type:"hint",text:"回复「编号 + 动作」就行：24 过 · 24 退 要改的地方"}]};
  const oldText={id:5,kind:"brief",title:"11/15 早报 · 会1场",text:"你要做\n#9 写周报 · 逾期1天",refs:[9,26],read:true,at:"2026-11-15T08:30:00",
    blocks:[{type:"text",text:"你要做\n#9 写周报 · 逾期1天\n等你验收\n#26 核对清单 · 清单已整理 [打开](https://x.feishu.cn/docx/a)\n其余3件 · [看板](https://x.feishu.cn/base/b) · 回复：26 过 / 26 退 意见"}]};
  evaluate(`D.inbox=${JSON.stringify([brief,oldCard,oldText])}; S.view="inbox"; S.inboxTab="全部";`);
  let page=evaluate("inboxPage()");
  assert.match(page,/onclick="openMsg\(7\)"/,"a brief about one item still opens as a brief");
  assert.doesNotMatch(page,/今天你要做/,"closed until clicked");
  evaluate("openMsg(7)");
  assert.equal(evaluate("D.inbox[0].read"),true);
  page=evaluate("inboxPage()");
  for (const words of ["今天的会 · 1 场","10:00  周会","今天你要做 · 1 件","AI 做好了，等你看 · 1 件","需要你定 · 1 件","还有 2 件","收起"]) assert.ok(page.includes(words),words);
  assert.match(page,/onclick="openItem\(26\)"/);
  assert.match(page,/<span class="pill amber">等你验收<\/span>/);
  assert.match(page,/act\(26,'approve'/,"same buttons as a list row");
  assert.match(page,/act\(24,'me'/); assert.match(page,/act\(24,'ai'/);
  assert.match(page,/act\(9,'done'/,"your own open item can be finished from the brief");
  assert.match(page,/href="https:\/\/x\.feishu\.cn\/docx\/a"[^>]*>打开文档/);
  assert.match(page,/href="https:\/\/vc\.feishu\.cn\/j\/1"[^>]*>进入会议 ↗/);
  assert.equal((page.match(/1 场会 · 你要做 1 件/g)||[]).length,1,"the first line is not repeated");
  await evaluate('apply(26,"approve",null)');               // a click in the brief acts like the list row
  page=evaluate("inboxPage()");
  assert.match(page,/<span class="pill green">已完成<\/span>/);
  assert.doesNotMatch(page,/act\(26,'approve'/);
  // The answer box opens in the brief it was clicked in, not on another row of the same item.
  evaluate('document.querySelector("#x").focus=()=>{}');
  evaluate('act(31,"answer","直接回答 AI 的问题",{closest:()=>({dataset:{msg:"6"}})})');
  assert.equal(evaluate("S.inline.at"),"6");
  assert.doesNotMatch(evaluate("inboxPage()"),/inline-31/,"brief 6 is closed");
  evaluate("openMsg(6)");
  page=evaluate("inboxPage()");
  assert.equal((page.match(/id="inline-31"/g)||[]).length,1);
  assert.doesNotMatch(page,/回「|回复「|退 要改的地方/,"old reply instructions are dropped, the whole footer too");
  assert.match(page,/什么时候做完？/); assert.match(page,/AI 卡住：缺收入导出/);
  assert.match(page,/act\(31,'answer'/);
  evaluate("S.inline=null; openMsg(5)");
  page=evaluate("inboxPage()");
  assert.match(page,/<div class="b-h">你要做<\/div>/);
  assert.match(page,/onclick="openItem\(9\)"/); assert.match(page,/逾期1天/);
  assert.doesNotMatch(page,/回复：/);
  assert.match(page,/>看板 ↗/);
  console.log("Client checks passed: new, running, cancel, restore, complete, closed-item conversation, stale status, concurrency copy, briefs opened in the inbox.");
})().catch(e => { console.error(e); process.exitCode=1; });
