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
  assert.match(evaluate("itemView(byN(45))"), /标为完成/);
  evaluate('byN(45).status="进行中"; S.working=true');
  assert.deepEqual(plain("detailButtons(byN(45)).map(b=>b[1])"), ["cancel","done"]);
  await evaluate('apply(45,"cancel",null)');
  assert.equal(evaluate("byN(45).status"), "取消");
  assert.equal(evaluate("isWorking(byN(45))"), false);
  assert.deepEqual(plain("statusOf(byN(45))"), ["已取消",""]);
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
  const pane = tab => evaluate(`S.setTab="${tab}"; sysPage()`).split('class="set-pane"')[1];
  const settings = ["tool","mode","ai","talk","store","notify","look","guide","adv"].map(pane).join("");
  for (const label of ["Slack","Notion","只存本机","本机 AI","整理","打开引导","跟随系统","接着做"]) assert.ok(settings.includes(label), label);
  assert.ok(!pane("ai").includes("setupAsk('slack')") && !pane("ai").includes("Notion"), "one settings page at a time");
  // the menu says what is chosen now, and a page only shows what follows from the choices made
  evaluate('D.settings={engine:"cindy",engine_sort:"codex",intake_mode:"manual"}; D.system={record:{feishu_ready:false,mode:"device"},store:{local:true}}');
  const menu = evaluate('S.setTab="tool"; sysPage()').split('class="set-pane"')[0];
  for (const label of ["驱动心响的 AI","接入","AI 做事","数据与通知","问题反馈","版本与更新","更多","都没接","Cindy","只存本机","平衡"]) assert.ok(menu.includes(label), label);
  assert.equal((menu.match(/class="cell /g)||[]).length, 7, "seven places in the settings, no more");
  // 聊天判断: every line read says what became of it, and why it was not taken
  evaluate('S.chatlog=[{who:"Carl",chat:"Slack · Carl",p2p:true,at:"2026-11-23 18:40",text:"好的",verdict:"drop",why:"只是客套话或表情"},{who:"Carl",chat:"Slack · Carl",p2p:true,at:"2026-11-23 14:24",text:"部署后通知我",verdict:"item",why:31}]');
  const log = evaluate('S.setTab="chatlog"; S.clQ="carl"; sysPage()');
  for (const label of ["没交给 AI","只是客套话或表情","建了事项","#31","‹ 接入"]) assert.ok(log.includes(label), label);
  evaluate('S.chatlog=null; S.clQ=""');
  assert.doesNotMatch(pane("store"),/飞书多维表格/, "no Feishu sheet without Feishu");
  // 记忆增强: off by default; on → the owner's own endpoint and key, a few suggested providers, a test button
  evaluate('D.settings.memory_boost={enabled:false,installed:true,presets:{siliconflow:{name:"硅基流动",base_url:"https://api.siliconflow.cn/v1",llm:"deepseek-ai/DeepSeek-V3",embed:"BAAI/bge-m3",dims:1024,key_url:"https://cloud.siliconflow.cn/account/ak"},deepseek:{name:"DeepSeek（向量另配）",base_url:"https://api.deepseek.com",llm:"deepseek-chat",embed:"",dims:1024}},status:{}}');
  let boost = pane("data");
  for (const label of ["记忆增强","Hy Memory","关着","boostSave({enabled:true})","心响自己的记忆照常工作"]) assert.ok(boost.includes(label), label);
  assert.doesNotMatch(boost,/bz-key/, "off: no form");
  evaluate('D.settings.memory_boost.enabled=true; D.settings.memory_boost.has_key=true; S.boost=null');
  boost = pane("data");
  for (const label of ["还没填全","硅基流动","DeepSeek（向量另配）","其他","bz-base","bz-key","已填好（要换就贴新的）","bz-embed","向量用另一家","boostTest()"]) assert.ok(boost.includes(label), label);
  evaluate('D.settings.memory_boost={...D.settings.memory_boost,ready:true,status:{written:12,waiting:3,stuck:1,error:"401 key 不对"}}');
  for (const label of ["在用","已写入 12 条，排队 3 条，1 条写不进去","最近出错：401 key 不对"]) assert.ok(pane("data").includes(label), label);
  evaluate('delete D.settings.memory_boost');
  assert.doesNotMatch(pane("mode"),/自动开工/, "manual mode has nothing to start by itself");
  assert.match(pane("ai"),/codex 命令行/); assert.doesNotMatch(pane("ai"),/claude 命令行/, "only the engines in use");
  // the page is just who does what: 做事 and 整理 each with its own choice, and only the AIs in use show their own setup
  assert.match(evaluate('S.setTab="ai"; sysPage()').split('class="set-pane"')[0],/Cindy \/ 本机 GPT/);
  evaluate('D.settings.engine_sort="cindy"');
  let one = pane("ai");
  assert.doesNotMatch(one,/分给了两个|setAll/, "no box urging one AI for all");
  assert.match(one,/叫不醒时谁来顶上/); assert.match(one,/codex 命令行/, "GPT stands in when Cindy cannot be woken: its row shows");
  assert.match(one,/有活时马上叫醒 Cindy/);
  evaluate('D.settings.engine_fallback=""');
  assert.doesNotMatch(pane("ai"),/codex 命令行/, "no GPT row once nothing uses it");
  evaluate('D.settings.engine_fallback="api"; D.settings.api={provider:"deepseek",key:false,target:["DeepSeek","https://api.deepseek.com/anthropic","deepseek-chat"],providers:[{id:"deepseek",name:"DeepSeek",model:"deepseek-chat",keys:"https://platform.deepseek.com/api_keys"},{id:"custom",name:"自定义"}]}');
  one = pane("ai");
  assert.match(one,/模型接口/); assert.match(one,/还没填 Key/); assert.match(one,/DeepSeek 后台/);
  evaluate('D.settings.wake={cindy:{name:"Cindy",hook:false,standin:{since:"2026-11-23T15:20:00+08:00",why:"Cindy 没开着",engine:"api"}}}');
  assert.match(pane("ai"),/现在由 模型接口 顶上（15:20 起）：Cindy 没开着/);
  evaluate('delete D.settings.engine_fallback; delete D.settings.api; delete D.settings.wake');
  assert.match(one,/class="chip on" onclick="setSetting\('engine','cindy'\)"/); assert.match(one,/class="chip on" onclick="setSetting\('engine_sort','cindy'\)"/);
  evaluate('D.settings.engine_sort="codex"');
  evaluate('D.settings={intake_mode:"all"}; D.system={record:{feishu_ready:true,mode:"feishu"}}');
  assert.match(pane("mode"),/自动开工/); assert.match(pane("store"),/飞书多维表格/);
  // setup check: every essential thing says whether it works, and what to do next when it does not
  evaluate('D.settings={engine:"codex",intake_mode:"all",me:{name:"",aliases:[]},engine_tests:{}}; D.system={listener:"没在运行",engines:{codex:false,claude:true},record:{feishu_ready:true,mode:"feishu"},store:{local:true},here:{platform:"mac",root:"/x/心响",python:"/x/py",lark_cli:true}}');
  assert.deepEqual(plain("setupProblems().map(x=>x.key)"),["listener","engine","me"]);
  let check = evaluate("checkList()");
  for (const label of ["还差 3 步才能用","复制给 AI 的话","copySetupPrompt()","不用自己动手","去填"]) assert.ok(check.includes(label), label);
  assert.doesNotMatch(check,/npm install|install-listen|class="cmd"/, "the owner is not handed commands: the AI gets them");
  assert.match(evaluate('S.setTab="check"; sysPage()').split('class="set-pane"')[0],/驱动心响的 AI<\/span><span class="val">[^<]* · 还差 3 步/);
  assert.match(evaluate('(S.view="all",S.all="今天",S.dim="type",allPage())'),/还差 3 步才能用/);
  evaluate('D.system.here.client=true'); assert.match(evaluate("checkList()"),/从开始菜单重新打开/); evaluate('D.system.here.client=false');
  evaluate('D.settings.me={name:"王小明",aliases:[]}; D.settings.engine_tests={codex:{ok:true,seconds:3,at:"11-20 10:00"}}; D.system.engines.codex=true; D.system.listener="运行中"');
  assert.deepEqual(plain("setupProblems()"),[]); assert.match(evaluate("checkList()"),/都配好了，可以用/); assert.doesNotMatch(evaluate('(S.view="all",S.all="今天",S.dim="type",allPage())'),/才能用/);
  evaluate('D.settings.engine_tests={}'); assert.match(evaluate("checkList()"),/testEngine\('codex'\)">测试连接/);
  evaluate('D.settings.intake_mode="manual"; D.settings.me={name:""}; D.settings.engine_tests={codex:{ok:true,seconds:3,at:"x"}}; D.system.record.feishu_ready=false');
  assert.deepEqual(plain("setupProblems()"),[], "nothing connected and manual: the name is not needed");
  for (const label of ["没接，可以不接","去接"]) assert.ok(evaluate("checkList()").includes(label), label);
  for (const label of ["setupAsk('feishu')","setupAsk('slack')","setupAsk('wechat')"]) assert.ok(pane("tool").includes(label), label);
  assert.doesNotMatch(pane("tool"),/want_feishu|want_slack|want_wechat/, "connecting is one click that goes to the AI, not a wish switch");
  // the guide's step for office tools: three plain choices when nothing is connected, no technical rows
  let step = evaluate('OB.find(o=>o.key==="tools").body(D.settings,D.system)');
  for (const label of ["setSetting('want_feishu',true)","setSetting('want_slack',true)","setSetting('want_wechat',true)",">飞书<",">Slack<",">微信<"]) assert.ok(step.includes(label), label);
  assert.doesNotMatch(step,/Token|后台程序|实时/, "nothing a newcomer cannot read");
  evaluate('D.settings.want_slack=true');
  assert.deepEqual(plain("setupProblems().map(x=>x.key)"),["me","tools"]); assert.match(evaluate("checkList()"),/你想接Slack/);
  evaluate('D.settings.want_slack=false; D.system.record.feishu_ready=true; D.settings.sources={meetings:true,chats:true,tasks:false}');
  step = evaluate('OB.find(o=>o.key==="tools").body(D.settings,D.system)');
  for (const label of ["飞书 · 已接入","setSetting('want_slack',true)"]) assert.ok(step.includes(label), label);
  assert.doesNotMatch(step,/后台程序|实时|want_feishu/);
  evaluate('D.system.record.feishu_ready=false; D.settings.sources={}');
  // WeChat: wanted is a step for the AI; installed and on shows whether it can be read
  evaluate('D.settings.want_wechat=true'); assert.deepEqual(plain("setupProblems().map(x=>x.key)"),["me","tools"]); assert.match(evaluate("checkList()"),/你想接微信/);
  assert.match(pane("tool"),/setupAsk\('wechat'\)/);
  evaluate('D.settings.want_wechat=false; D.settings.me={name:"王小明"}; D.system.wechat={installed:true,enabled:true,status:{ok:true,at:"11-20 14:00",behind:true}}');
  let wx = pane("tool");
  for (const label of ["读微信","setSetting('wechat_enabled',false)","上次读取 11-20 14:00","只看得到每个聊天的最后一条","wechatCheck()"]) assert.ok(wx.includes(label), label);
  assert.deepEqual(plain("setupProblems()"),[]); assert.match(evaluate("checkList()"),/已接入微信/);
  assert.match(evaluate('S.setTab="tool"; sysPage()').split('class="set-pane"')[0],/接入<\/span><span class="val">微信/);
  evaluate('D.system.wechat.status={ok:false,error:"微信没有打开",at:"11-20 14:02"}');
  assert.deepEqual(plain("setupProblems().map(x=>x.key)"),["tools"]); assert.match(pane("tool"),/微信没有打开/);
  evaluate('D.system.wechat=null; D.settings.me={name:""}');
  evaluate('D.settings.want_feishu=true');   // the owner means to connect Feishu: that is now a step, and the name is needed
  assert.deepEqual(plain("setupProblems().map(x=>x.key)"),["me","tools"]); assert.match(evaluate("checkList()"),/你想接飞书/);
  evaluate('D.settings.want_feishu=false');
  assert.deepEqual(plain("OB.map(o=>o.key)"),["me","tools","know","ai"], "who you are, where work comes from, what the AI may look at, then one message to your AI");
  // a newer version: one button, and a mark in the sidebar; a copy under development says why it does not update itself
  evaluate('D.system.update={current:"0.4.0",latest:"0.4.1",newer:true,can:true,kind:"client",checked:"11-20 12:00"}');
  let up = pane("update");
  for (const label of ["现在的版本　0.4.0","updateNow()","更新到 0.4.1","检查更新","数据不动"]) assert.ok(up.includes(label), label);
  assert.match(evaluate('S.setTab="update"; sysPage()').split('class="set-pane"')[0],/版本与更新<\/span><span class="val">有新版 0.4.1/);
  assert.match(evaluate("sideHTML()"),/title="有新版本 0.4.1">更新</);
  evaluate('D.system.update={current:"0.4.1",latest:"0.4.1",newer:false,can:false,kind:"git",why:"这份是开发中的版本"}');
  up = pane("update"); assert.match(up,/已是最新/); assert.doesNotMatch(up,/updateNow/); assert.match(up,/开发中的版本/);
  assert.doesNotMatch(evaluate("sideHTML()"),/>更新</);
  evaluate('D.system.update={current:"0.4.0",latest:"0.4.1",newer:true,can:true,doing:"正在下载 0.4.1…"}'); assert.match(pane("update"),/正在下载 0\.4\.1…/);
  evaluate('D.system.update=null');
  // deliverables: each version can be corrected or removed, one can be added; the item follows the newest
  const keepD = evaluate("JSON.stringify([D.items,D.deliverables,D.threads])");
  evaluate('D.items=[{n:9,title:"写方案",status:"待验收",who:"AI",url:"https://x/docx/AI"}]; D.deliverables={9:[{v:1,at:"11-20 10:00",summary:"AI 写的",url:"https://x/docx/AI"}]}; D.threads={9:[]}; S.dvEdit=null');
  let dv = evaluate("nextCard(byN(9))");
  for (const label of ["v1","AI 写的","打开文档 ↗","全部版本（1）","dvOpen(9,1)","编辑","删掉","＋ 自己加一版","验收通过"]) assert.ok(dv.includes(label), label);
  assert.doesNotMatch(dv, /<details class="nc-vers" open/, "the versions are folded away");
  evaluate('S.dvEdit={n:9,v:1}'); assert.match(evaluate("nextCard(byN(9))"),/<details class="nc-vers" open[\s\S]*id="dv-url"[^>]*value="https:\/\/x\/docx\/AI"/);
  await evaluate('dvSend(9,{op:"edit",v:1,url:"https://x/docx/MINE",summary:"我自己建的"},"改好了")');
  assert.deepEqual(plain("[D.deliverables[9][0].url,D.deliverables[9][0].summary,byN(9).url,S.dvEdit]"),["https://x/docx/MINE","我自己建的","https://x/docx/MINE",null]);
  await evaluate('dvSend(9,{op:"add",url:"https://x/docx/V2",summary:""},"加上了")');
  assert.deepEqual(plain("[D.deliverables[9].map(v=>v.v),byN(9).url]"),[[1,2],"https://x/docx/V2"]);
  await evaluate('dvSend(9,{op:"remove",v:2},"删掉了")'); await evaluate('dvSend(9,{op:"remove",v:1},"删掉了")');
  assert.deepEqual(plain("[D.deliverables[9],byN(9).url]"),[[],null]); assert.doesNotMatch(evaluate("nextCard(byN(9))"),/nc-vers|nc-dv/);
  evaluate("S.more=9"); assert.match(evaluate("itemView(byN(9))"),/dvOpen\(9,0\);return false">填交付链接/); evaluate("S.more=null");
  // another app for one item: offered there, and the usual one in settings is not what changes
  evaluate('D.settings={pickup_app:"cindy",apps:{cindy:true,claude:true,"terminal-codex":true,"terminal-claude":false}}; S.pickApp=null');
  assert.match(evaluate("sessionsTab(byN(9))"),/S\.pickApp=9;render\(\)">换一个 App/); assert.doesNotMatch(evaluate("sessionsTab(byN(9))"),/go\('sys'\)/);
  evaluate('S.pickApp=9'); let apps = evaluate("sessionsTab(byN(9))");
  for (const label of ["只这件事换，默认不变","pickUp(9,'claude')","pickUp(9,'terminal-codex')"]) assert.ok(apps.includes(label), label);
  assert.doesNotMatch(apps,/pickUp\(9,'cindy'\)|terminal-claude/); assert.equal(evaluate("D.settings.pickup_app"),"cindy");
  evaluate(`[D.items,D.deliverables,D.threads]=${keepD}; D.settings={}; S.pickApp=null`);
  // a problem can be written down from anywhere, and they are handed in together
  evaluate('D.bugs=[]; S.bugging=null');
  assert.match(evaluate("sideHTML()"),/onclick="bugOpen\(\)">反馈问题</);
  evaluate('S.bugging={}'); assert.match(evaluate("bugView()"),/id="bug-text"/);
  evaluate('S.bugging=null; D.bugs=[{id:2,at:"11-20 15:20",text:"更新一直转圈",status:"open"},{id:1,at:"11-20 15:14",text:"窗口太窄",status:"sent"}]');
  assert.match(evaluate("sideHTML()"),/反馈问题　已记 1 条/);
  let bugs = pane("bugs");
  for (const label of ["1 条待提交","更新一直转圈","窗口太窄","已提交","bugHandIn('copy')","bugHandIn('github')","bugOp({remove:2})"]) assert.ok(bugs.includes(label), label);
  assert.match(evaluate('S.setTab="bugs"; sysPage()').split('class="set-pane"')[0],/问题反馈<\/span><span class="val">1 条待提交/);
  await evaluate("bugOp({remove:2})"); assert.doesNotMatch(pane("bugs"),/bugHandIn/);
  evaluate('D.bugs=null');
  // an update that never landed says so instead of spinning for ever
  evaluate('D.system.update={current:"0.4.2",latest:"0.4.6",newer:true,can:true,failed:"更新没有完成。再点一次试试"}');
  assert.match(pane("update"),/更新没有完成/); assert.match(pane("update"),/onclick="updateNow\(\)">更新到 0\.4\.6/);
  evaluate('D.system.update=null');
  // Feishu connected on paper but unreadable: it is a step, with the reason, wherever "connected" was shown
  evaluate('D.settings={engine:"codex",intake_mode:"all",me:{name:"王小明"},engine_tests:{codex:{ok:true,at:"x"}}}; D.system={listener:"运行中",engines:{codex:true},record:{feishu_ready:true,mode:"feishu",feishu_broken:{why:"飞书登录过期了，要重新登录一次",since:"11-20 14:36"}},store:{local:true},here:{platform:"windows",client:true}}');
  assert.deepEqual(plain("setupProblems().map(x=>x.key)"),["feishu"]);
  for (const label of ["还差 1 步才能用","飞书登录过期了","11-20 14:36","都收不进来"]) assert.ok(evaluate("checkList()").includes(label), label);
  assert.match(pane("tool"),/现在读不到/); assert.match(evaluate('(S.view="all",S.all="今天",S.dim="type",allPage())'),/还差 1 步才能用/);
  assert.match(evaluate('S.setTab="tool"; sysPage()').split('class="set-pane"')[0],/飞书（读不到）/);
  evaluate('D.system.record.feishu_broken=null'); assert.deepEqual(plain("setupProblems()"),[]); assert.doesNotMatch(pane("tool"),/现在读不到/);
  // any AI the message was sent to: it says who it is, shows up by name, and is missed when it stops asking
  evaluate('D.settings={engine:"driver:Antigravity",engine_sort:"driver:Antigravity",intake_mode:"all",me:{name:"王小明"},engine_tests:{},drivers:[{name:"Antigravity",app:"Antigravity",model:"gemini-3",seen:"11-20 16:40",online:true},{name:"Claude Code",app:"终端",model:"opus",seen:"11-20 09:00",online:false}]}; D.system={listener:"运行中",engines:{codex:false,claude:false,gemini:false},record:{feishu_ready:true,mode:"feishu"},store:{local:true},here:{platform:"windows",client:true}}');
  assert.deepEqual(plain("setupProblems()"),[]);
  let any = pane("ai");
  for (const label of ["做事","整理","copySetupPrompt()","Antigravity","gemini-3","在这儿","Claude Code（不在）","setSetting('engine','driver:Claude Code')","copyPart('work')","copyPart('sort')"]) assert.ok(any.includes(label), label);
  let guide = evaluate('S.ob={step:3,started:true}; obView()');
  for (const label of ["AI 接手","Antigravity 在这儿","copySetupPrompt()","共 ","pg-bar"]) assert.ok(guide.includes(label), label);
  assert.match(evaluate('S.setTab="ai"; sysPage()').split('class="set-pane"')[0],/驱动心响的 AI<\/span><span class="val">Antigravity</);
  assert.match(evaluate("checkList()"),/做事的 AI：Antigravity/); assert.match(evaluate("checkList()"),/按定时任务来领活（gemini-3）/);
  evaluate('D.settings.drivers[0].online=false');
  assert.deepEqual(plain("setupProblems().map(x=>x.key)"),["engine"]); assert.match(evaluate("checkList()"),/现在不在（上次来是 11-20 16:40）/);
  evaluate('D.settings={engine:"codex",intake_mode:"manual",me:{name:""},engine_tests:{},drivers:[]}');
  assert.match(evaluate("checkList()"),/不用装：把上面那段话发给你常用的 AI/);
  // three local AIs and Cindy, for doing items and for sorting
  evaluate('D.settings={engine:"gemini",engine_sort:"cindy",engine_fallback:"gemini",intake_mode:"all",me:{name:"王小明"},engine_tests:{},cindy:{installed:true,ready:true,same_project:true,projects:[],schedule:{status:"active",hooked:true}}}; D.system={listener:"运行中",engines:{codex:true,claude:true,gemini:true},record:{feishu_ready:true,mode:"feishu"},store:{local:true},here:{platform:"mac"}}');
  let ai = pane("ai");
  for (const label of ["本机 Gemini","gemini 命令行","API Key","setSetting('engine_sort','cindy')","Cindy 定时来领活"]) assert.ok(ai.includes(label), label);
  assert.doesNotMatch(ai,/codex 命令行/, "only the engines in use (Gemini also stands in for Cindy)");
  assert.deepEqual(plain("setupChecks().filter(x=>['engine','sort'].includes(x.key)).map(x=>[x.key,x.state])"),[["engine","todo"],["sort","ok"]]);
  evaluate('D.system.engines.gemini=false'); assert.match(evaluate("checkList()"),/还没有装 本机 Gemini/);
  // 经验库: a topic per memory, its index line and its text; the owner edits it, the AI only drafts
  evaluate('D.memory={library:[{name:"做事通用规矩",description:"每件事动手前都读",sections:["先给结论"],raw:"name：做事通用规矩\\ndescription：每件事动手前都读\\n\\n## 先给结论\\n第一屏给结论"}],waiting:[{id:7,text:"年卡要不要打折",wait_for:"付费数据",source:"周会",at:"11-19"}],recent:[]}; S.memTab="rules"');
  let mem = evaluate("memPage()");
  assert.match(mem,/关于你/); assert.match(mem,/<textarea id="prof"/, "nothing written yet: a box to write it, with an example");
  evaluate('D.memory.profile={text:"岗位：产品经理",at:"11-21 10:00"}'); let mem2 = evaluate("memPage()");
  assert.match(mem2,/岗位：产品经理/); assert.doesNotMatch(mem2,/<textarea id="prof"/); assert.match(mem2,/S\.profEdit=true/);
  // 让 AI 认识我: tick the places, agree, the AI looks; its draft waits for the owner
  evaluate('D.memory.profile={text:""}; S.scanPick=SCAN.filter(x=>scanReady(x[0])).map(x=>x[0])'); let pick = evaluate("memPage()");
  for (const label of ["让 AI 看哪些地方","飞书近 30 天","本机项目文件夹","微信近期聊天","Cindy/Claude 记忆","同意，开始看","不保存聊天原文"]) assert.ok(pick.includes(label), label);
  evaluate('S.scanPick=null; D.memory.profile={text:"",scanning:{by:"Antigravity",step:"2/4 看飞书文档"}}'); assert.match(evaluate("memPage()"),/Antigravity 正在认识你<\/b>：2\/4 看飞书文档/);
  evaluate('D.memory.profile={text:"",draft:{text:"## 岗位和职责\\n产品经理",by:"Antigravity",at:"11-21 10:00"}}'); let dr = evaluate("memPage()");
  for (const label of ["Antigravity 写好了一份「关于你」草稿","产品经理","useDraft(true)","editDraft()","useDraft(false)"]) assert.ok(dr.includes(label), label);
  evaluate('D.memory.profile.text="已经确认的正式版"');
  const archived=evaluate("memPage()");
  assert.match(archived,/查看旧版草稿/); assert.match(archived,/核对新变化/);
  assert.doesNotMatch(archived,/useDraft\(true\)/);
  evaluate('D.memory.profile=null');
  // 项目与版本 and scoped rules
  assert.match(evaluate("memPage()"),/让 AI 整理项目和版本/, "nothing sorted yet: one button");
  evaluate("globalThis.keptLib=D.memory.library");
  evaluate(`D.memory.versions=[{id:1,project_id:1,project:"示例 App",name:"2.3",status:"current",status_label:"当前",period:"11 月",note:"会员改版",items:[{n:45,title:"定积分规则",status:"完成",by:"AI"}],expected:[{id:7,title:"上线前验收",why:"每个版本都要",status:"open",due:"2026-11-28"}]},{id:2,project_id:1,project:"示例 App",name:"2.2",status:"done",status_label:"已完成",items:[],expected:[]}]; D.memory.verdicts={"通过":3,"退回":1,"要改":0}`);
  let ver = evaluate("memPage()");
  for (const label of ["项目与版本","示例 App","2.3","当前","会员改版","#45","AI 归的","1/1 件做完","预计会遇到","上线前验收",">11-28<","organize({expected:7,status:'done'})","<details class=\"ver\">","重新整理","近 30 天评审：通过 3 · 退回 1 · 要改 0"]) assert.ok(ver.includes(label), label);
  evaluate('D.memory.versions=[]; D.memory.verdicts={}; D.memory.library=globalThis.keptLib');
  // 效果: below the rules; nothing reviewed yet → one plain line, no chart
  let eff0 = evaluate("memPage()");
  assert.match(eff0,/经验库[\s\S]*效果[\s\S]*验收过几次交付后，这里会显示 AI 的表现/); assert.doesNotMatch(eff0,/一次通过率|<svg/);
  evaluate(`D.memory.effect={has:true,recent:{total:5,first:4,rate:80},before:{total:4,first:2,rate:50},rounds:0.6,blocked:2,failed:1,
    weeks:[{start:"09-28",total:0,first:0,rate:null},{start:"10-05",total:1,first:0,rate:0},{start:"10-12",total:2,first:1,rate:50},{start:"10-19",total:1,first:1,rate:100},
           {start:"10-26",total:1,first:1,rate:100},{start:"11-02",total:2,first:1,rate:50},{start:"11-09",total:1,first:1,rate:100},{start:"11-16",total:1,first:1,rate:100}],
    by_type:[{name:"方案",total:4,first:3,rate:75,few:false},{name:"报告<b>",total:1,first:1,rate:100,few:true}],by_ai:[{name:"Claude",total:5,first:4,rate:80,few:false}],by_project:[],
    reasons:[{kind:"太长/格式",count:3,example:"写得太长"},{kind:"方向不对",count:2,example:""},{kind:"缺数据/背景",count:1,example:"没数据"},{kind:"其他",count:1,example:""}]}`);
  let eff = evaluate("memPage()");
  for (const label of ["最近 4 周一次通过率 80%","（前 4 周 50%）","<svg","11-16","按交付类型","方案","75%","样本太少","按 AI","Claude","通过前平均要改 0.6 轮","待答复 2 次","失败 1 次","太长/格式","比如「写得太长」","缺数据/背景"]) assert.ok(eff.includes(label), label);
  assert.ok(!eff.includes("报告<b>") && eff.includes("报告&lt;b&gt;"), "names are escaped");
  assert.doesNotMatch(eff,/其他 <span/, "only the top three reasons");
  assert.equal((eff.match(/<rect /g)||[]).length, 8, "one mark per week");
  evaluate('D.memory.effect={has:true,recent:{total:2,first:1,rate:50},before:{total:0,first:0,rate:null},weeks:[],by_type:[],by_ai:[],reasons:[],blocked:0,failed:0}');
  assert.match(evaluate("memPage()"),/最近 4 周一次通过率 50%.*（前 4 周 没有验收）/);
  evaluate('delete D.memory.effect');
  // 记忆更新: what the daily tidy proposed waits on top, above 关于你; nothing when there is none
  assert.doesNotMatch(evaluate("memPage()"),/记忆更新/);
  evaluate(`D.memory.updates=[{id:4,type:"memory",label:"经验库",content:"「做事通用规矩」加上：对外文案先给两版",why:"说的是一件事",detail:"加上：\\n## 对外文案先给两版"},{id:5,type:"profile",label:"关于你",content:"## 在做的事\\n年卡",why:"最近都在谈",old:"## 在做的事\\n会员"}]`);
  let upd = evaluate("memPage()");
  for (const label of ["记忆更新 · 2 条","经验库","加上：对外文案先给两版","看要改的原文","## 对外文案先给两版","说的是一件事","memUpdate({use:4})","memUpdate({drop:4})","原来：## 在做的事","memUpdate({use:'all'})","全部用上"]) assert.ok(upd.includes(label), label);
  assert.ok(upd.indexOf("记忆更新") < upd.indexOf("关于你"), "above 关于你");
  assert.match(evaluate("sideHTML()"),/记忆<span class="c need">2</);
  await evaluate("memUpdate({drop:4})"); assert.match(evaluate("memPage()"),/记忆更新 · 1 条/);
  await evaluate("memUpdate({use:'all'})"); assert.doesNotMatch(evaluate("memPage()"),/记忆更新/);
  for (const label of ["经验库","做事通用规矩","每件事动手前都读","1 节","第一屏给结论","S.libEdit=0","libDelete(0)","新建一份"]) assert.ok(mem.includes(label), label);
  evaluate("S.libEdit=-1"); assert.match(evaluate("memPage()"),/<textarea id="libtext"[\s\S]*description：要……/); evaluate("S.libEdit=undefined");
  assert.doesNotMatch(evaluate("sideHTML()"),/记忆<span class="c need">/);
  // 记忆 holds only the rules; what a meeting left waiting or decided is shown with the items
  assert.doesNotMatch(mem,/年卡要不要打折|在等的|会上的结论/);
  evaluate('D.memory.recent=[{label:"还没定",text:"投流方案怎么定",source:"周会",at:"11-19"},{label:"定了",text:"先做周视图",source:"周会",at:"11-19"}]; S.all="全部"; S.dim="type"; S.laterOpen=true');
  let later = evaluate("allPage()");
  for (const label of ["年卡要不要打折","ruleOp({waiting_done:7})","投流方案怎么定","以后要确认的<span class=\"n\">2<"]) assert.ok(later.includes(label), label);
  assert.doesNotMatch(later,/先做周视图/, "what is decided is not something to confirm later");
  assert.doesNotMatch(evaluate('S.all="今天"; allPage()'),/以后要确认的/, "a folded block at the bottom of 全部, not a filter of its own"); evaluate('S.all="全部"; S.laterOpen=false');
  assert.doesNotMatch(evaluate("allPage()"),/会上的结论/);
  evaluate('D.memory=null; D.settings={}; D.system={}; S.memTab=null');
  // projects: the owner's own sorting; a guess is marked, and moving is one call
  evaluate('S.dim="proj"; S.all="全部"');
  const keep = evaluate("JSON.stringify(D.items)");
  evaluate('D.projects=[{id:1,name:"数据看板",color:"#5856d6"},{id:2,name:"招人",color:"#ff9500"}]; D.items=[{n:1,title:"写标签文档",status:"进行中",who:"AI",project:1,project_by:"本人"},{n:2,title:"标签更新",status:"待验收",who:"AI",project:1,project_by:"猜"},{n:3,title:"发招聘",status:"进行中",who:"我"}]; S.all="进行中"; S.proj="all"');
  let all = evaluate("allPage()");
  for (const label of ["全部项目","数据看板","招人","未归类","新建项目","多选"]) assert.ok(all.includes(label), label);
  assert.match(all,/数据看板<\/span><span class="val need">1 件等你/);
  evaluate("S.more=2"); assert.match(evaluate("itemView(byN(2))"),/系统猜的/); evaluate("S.more=1"); assert.doesNotMatch(evaluate("itemView(byN(1))"),/系统猜的/); evaluate("S.more=null");
  evaluate("S.more=3"); assert.match(evaluate("itemView(byN(3))"),/未归类/); evaluate("S.more=null");
  await evaluate("moveItems([2,3],2)");
  assert.deepEqual(plain("D.items.map(i=>[i.project,i.project_by])"),[[1,"本人"],[2,"本人"],[2,"本人"]]);
  evaluate('S.proj=2'); assert.doesNotMatch(evaluate("allPage()").split('class="proj-head"')[1],/写标签文档/);
  evaluate('S.picking=true; togglePick(1); S.moving=[...S.pick]');
  assert.match(evaluate("moveView()"),/移到哪个项目/); assert.match(evaluate("moveView()"),/moveItems\(\[1\],0\)/);
  await evaluate("moveItems([1],0)");
  assert.deepEqual(plain("[byN(1).project,S.picking,S.moving,S.pick.size]"),[null,false,null,0]);
  await evaluate("removeProject(2)");
  assert.deepEqual(plain("[D.projects.length,byN(3).project,S.proj]"),[1,null,"all"]);
  evaluate(`D.items=${keep}; D.projects=[]; S.proj="all"`);
  { const none=evaluate("allPage()"); assert.doesNotMatch(none,/未归类|多选|全部项目/, "no projects yet: nothing to pick from");
    assert.equal((none.match(/＋ 新建项目/g)||[]).length, 1, "one place to make the first project"); }
  assert.match(evaluate("allPage()"),/openNewProject\(\)"><span class="lab">＋ 新建项目/);
  evaluate("S.newProj=true"); assert.match(evaluate("allPage()"),/id="proj-name"/); evaluate("S.newProj=false");
  // 事项 is the one page: 今天 is a filter, and by type: 要你看, 等你定 (grouped by where it came from), 看起来已经办完, 你来做, AI 做, 别人做
  const keepAll = evaluate("JSON.stringify([D.items,D.inbox])");
  evaluate(`D.items=[{n:1,title:"等验收的事",status:"待验收",who:"AI"},{n:2,title:"问你的事",status:"卡住",who:"AI",blocker:"按哪个口径"},{n:3,title:"今天要交的事",status:"进行中",who:"我",due:TODAY},{n:4,title:"排队的事",status:"进行中",who:"AI"},{n:5,title:"别人的事",status:"进行中",who:"别人"},{n:6,title:"下周的事",status:"进行中",who:"我",due:"2099-01-01"}];
    D.inbox=[{id:1,at:TODAY+"T09:00:00",kind:"done",title:"#1 做完了",refs:[1],read:false},{id:2,at:TODAY+"T08:30:00",kind:"brief",title:"早报",text:"早报\\n第二行",read:false},{id:3,at:"2026-01-01T08:30:00",kind:"pending",title:"「投流方案」该定了",read:false}]; S.view=null; S.all="今天"; S.dim="type"; S.openMsg=null; S.working=false; render()`);
  assert.equal(evaluate("S.view"),"all", "事项 opens first");
  const today = evaluate("allPage()");
  for (const label of [">最近<",">通讯录<",">分类<",">项目<",">今天<",">全部<",">已结束<","待我处理","等验收的事","问你的事","我负责","今天要交的事","提醒","投流方案"]) assert.ok(today.includes(label), label);
  assert.doesNotMatch(today,/别人的事|下周的事|排队的事|AI 自己判断能不能做/, "今天: only today's; the AI switch is in settings");
  assert.deepEqual([...today.matchAll(/class="sec-h"[^>]*>([^<]+)</g)].map(m=>m[1]),["提醒","待我处理","我负责"], "what waits on you first");
  const everything = evaluate('S.all="全部"; allPage()');
  for (const label of ["AI 负责","排队的事","他人负责","别人的事","下周的事"]) assert.ok(everything.includes(label), label);
  assert.equal(evaluate("needCount()"),2);
  const side = evaluate("sideHTML()");
  assert.match(side,/事项<span class="c hot">2</); assert.doesNotMatch(side,/今天|新进来|收件箱|等我处理/);
  assert.doesNotMatch(today,/今天 \d|消息 <b>|以后要确认的/, "one row of controls, no numbers on it");
  assert.match(side,/>消息</, "消息 has its own quiet place in the menu");
  evaluate('S.view="inbox"'); assert.match(evaluate("sideHTML()"),/class="nav on" onclick="go\('inbox'\)"/);
  evaluate('S.view="today"; render()'); assert.equal(evaluate("S.view"),"all", "an old link to 今天 lands on 事项");
  // 等你定: grouped by where they came from, a group taken or dropped at once; 备材料 sits inside its item
  evaluate(`D.items=[
    {n:11,title:"暂停代运营",status:"待确认",who:"我",created:"2026-11-25 10:00",origin:{kind:"会议",group:"观己日常运营沟通",person:null,client:false,at:"2026-11-25 10:00"},quote:"[00:05:13]  00:05:13   代运营先停"},
    {n:12,title:"试投搜索广告",status:"待确认",who:"我",created:"2026-11-25 10:01",origin:{kind:"会议",group:"观己日常运营沟通",person:null,client:false,at:"2026-11-25 10:00"}},
    {n:13,title:"给实习生排工作",status:"待确认",who:"我",created:"2026-11-26 09:00",origin:{kind:"聊天",group:"陈娈娇",person:"陈娈娇",client:false,at:"2026-11-26 09:45"},quote:"[陈娈娇 2026-11-26 09:45 陈娈娇] 你直接跟她说一声就行"},
    {n:14,title:"客户要发票",status:"待确认",who:"我",created:"2026-11-26 10:00",origin:{kind:"聊天",group:"曼朗外部群",person:"王客户",client:true,at:"2026-11-26 10:00"}},
    {n:15,title:"告诉实习生安排",status:"进行中",who:"我",due:TODAY,created:"2026-11-26 09:46",origin:{kind:"聊天",group:"陈娈娇",person:"陈娈娇",client:false,at:"2026-11-26 09:45"},quote:"[陈娈娇 2026-11-26 09:45 陈娈娇] 你直接跟她说一声就行，谢谢~"},
    {n:16,title:"备材料：告诉实习生安排",status:"待验收",who:"AI",parent:15},
    {n:17,title:"让豪铭加返回按钮",status:"未开始",who:"我",done_hint:{evidence:"[11-25 14:02 李瑞轩] 返回按钮加上了",where:"和李瑞轩的私聊"}}];
    D.inbox=[{id:9,at:TODAY+"T09:06:00",kind:"system",title:"⚠ AI 员工已经 269 分钟没检查会议和事项了。",read:false}]; S.view="all"; S.all="全部"; S.dim="type"; render()`);
  const groups = plain("freshGroupsOf(D.items.filter(i=>i.status==='待确认')).map(g=>[g.label,g.tag,g.items.map(i=>i.n)])");
  assert.deepEqual(groups,[["王客户","客户",[14]],["陈娈娇","聊天",[13]],["观己日常运营沟通","会议",[11,12]]]);
  const t2 = evaluate("allPage()");
  for (const label of ["待分配","全部我负责","全部交给 AI","全部取消","观己日常运营沟通","2 件"]) assert.ok(t2.includes(label), label);
  const heads = [...t2.matchAll(/class="sec-h"[^>]*>([^<]+)</g)].map(m=>m[1]);
  assert.deepEqual(heads,["待我处理","待分配","疑似已完成"], "the sections in order; an item sits in the first one it fits");
  assert.ok(t2.includes("告诉实习生安排") && t2.includes("AI 已备好材料，待验收"), "the 备材料 shows inside its item");
  assert.doesNotMatch(t2,/备材料：告诉实习生安排/, "not as a row of its own");
  assert.ok(t2.includes("陈娈娇 11/26 09:45：「你直接跟她说一声就行，谢谢~」"), "why it is here: who said what, when");
  assert.ok(t2.includes("返回按钮加上了") && t2.includes("未完成"), "done, with the words that show it");
  assert.doesNotMatch(t2,/269 分钟/, "no system notice in the list");
  // the status dot: the system's own trouble, handled in the background
  assert.match(evaluate("healthDot()"),/需你处理 \d+ 项/, "what only the owner can do comes first");
  assert.match(evaluate("healthDot(0)"),/自动修复中/);
  assert.match(evaluate("S.health=true; healthPanel()"),/269 分钟没检查[\s\S]*无需处理/); evaluate("S.health=false");
  evaluate("D.inbox=[]"); assert.equal(evaluate("healthDot(0)"),"", "all well: no dot at all");
  // 事项: the left column switches how to look (最近 / 通讯录 / 分类 / 项目), no switch at the top right.
  // 最近: the people, meetings, groups with something on, mixed, what waits on you first. 通讯录: everyone dealt with.
  evaluate(`S.view="all"; S.dim=null; S.all="全部"; S.who=null; S.ent=null; S.pcOpen=null; D.people=[{name:"许佳婷",kind:"同事",guessed:true,messages:7,meetings:["m1"],chats:[],last:"2026-11-26 14:06",
    text:"## 是谁\\n- 测试，负责走查（11-24 例会）",note:"",understood:"2026-11-26T14:52:00",enough:true}];
    D.meetings=[{id:"m1",topic:"AI项目例会",at:"2026-11-24 09:15",url:"https://x.test/m",people:["许佳婷","陈娈娇"]}]`);
  const recent = evaluate("allPage()");
  for (const label of [">最近<",">通讯录<",">分类<",">项目<","王客户","陈娈娇","找人、会议、群"]) assert.ok(recent.includes(label), label);
  assert.doesNotMatch(recent, /怎么分组|按类型/, "the way to look is switched on the left");
  evaluate('S.ent="p:王客户"'); assert.match(evaluate("allPage()"),/不是客户/);
  evaluate('setDim("people")'); const book = evaluate("allPage()");
  for (const label of ["找人","许佳婷","陈娈娇","王客户"]) assert.ok(book.includes(label), label);
  assert.ok(book.indexOf(">客户<") < book.indexOf(">同事<"), "clients first");
  evaluate('S.who="陈娈娇"'); const chen = evaluate("allPage()");
  assert.match(chen,/标成客户/); assert.match(chen,/AI 空闲时会写一份/, "nobody written about yet says so");
  evaluate('S.who="许佳婷"'); const folded = evaluate("allPage()");
  assert.ok(folded.includes("测试，负责走查") && folded.includes(">展开<"), "one line on who they are");
  assert.doesNotMatch(folded, /你写的|心响对TA的理解/, "the rest is folded away");
  evaluate('S.pcOpen="许佳婷"'); const xu = evaluate("allPage()");
  for (const label of ["心响对TA的理解","重新理解","你写的","以这里为准","一起开过的会","AI项目例会","聊天 7 条",">收起<"]) assert.ok(xu.includes(label), label);
  evaluate('openMeeting("m1")'); assert.equal(plain("[S.dim,S.ent]").join(), "recent,m:m1");
  const meet = evaluate("allPage()");
  for (const label of ["AI项目例会","参会","许佳婷","陈娈娇","会议记录 ↗","这场会收进来的事"]) assert.ok(meet.includes(label), label);
  evaluate('openPerson("陈娈娇")'); assert.equal(plain("[S.dim,S.who]").join(), "people,陈娈娇", "an attendee opens their page");
  evaluate('setDim("type")'); assert.match(evaluate("allPage()"),/scrollIntoView/, "分类: the sections on the left");
  evaluate("D.people=[]; D.meetings=[]; S.pcOpen=null");
  evaluate('S.dim="proj"; S.who=null');
  evaluate(`[D.items,D.inbox]=${keepAll}; S.view="all"; S.all="今天"; S.dim="type"`);
  assert.ok(!/62 改|输入指令|编号 \+ 动作/.test(html), "no command syntax shown to the owner");
  for (let step = 0; step < evaluate("OB.length"); step++) {  // the first-run guide: every step renders its own title
    evaluate(`S.ob={step:${step}}`);
    assert.ok(evaluate("obView()").includes(evaluate(`OB[${step}].title`)));
  }
  // the guide's last step and 今天 show each step the AI is on, how many are left and about how long
  evaluate('D.settings={...D.settings,engine:"driver:Antigravity",drivers:[{name:"Antigravity",online:true,model:"gemini-3"}],want_feishu:true,setup:{driver:{name:"Antigravity",online:true},asks:{feishu:{at:"x"}},doing:{feishu:{by:"Antigravity",step:"2/4 登录飞书",said:"请在浏览器里点授权"}},gave_up:{}}}; D.system={...D.system,listener:"运行中",record:{feishu_ready:false}}; D.memory={...D.memory,profile:{text:"",asked:{sources:["folders"]}}}');
  let board = evaluate("progressBoard(setupSteps())");
  for (const label of ["第 3 步，共 4 步","大约还要 10 分钟","接入飞书","2/4 登录飞书","要你做：请在浏览器里点授权","认识你","排着队"]) assert.ok(board.includes(label), label);
  evaluate('D.memory.organize={asked:{at:"x"}}'); board = evaluate("progressBoard(setupSteps())");
  for (const label of ["共 5 步","整理项目和版本","大约还要 14 分钟"]) assert.ok(board.includes(label), label);
  evaluate('D.memory.organize={}; D.memory.versions=[{id:1}]'); board = evaluate("progressBoard(setupSteps())");
  assert.match(board,/go\('mem'\)[^>]*>理出 1 个版本，去「记忆」里看/, "finished: one click to see it");
  evaluate('D.memory.versions=[]; S.organizedNow=false');
  assert.match(evaluate('(S.view="all",S.all="今天",S.dim="type",allPage())'),/pg-card/, "today shows the progress while the AI works");
  evaluate('D.settings.setup={driver:{},asks:{},doing:{},gave_up:{}}; D.settings.want_feishu=false; D.memory.profile={text:"x"}');
  assert.doesNotMatch(evaluate('(S.view="all",S.all="今天",S.dim="type",allPage())'),/pg-card/, "nothing going on: no progress card");
  // who sees the guide: a new owner; an owner from an older guide who was never got to know; the rest only hear what is new
  for (const [st, known, ob, news] of [[{onboarded:false},false,true,false],[{onboarded:true,guide_version:1},false,true,false],[{onboarded:true,guide_version:1},true,false,true],[{onboarded:true,guide_version:3},true,false,false],[{onboarded:true,guide_version:2},true,false,true]]) {
    const g = plain(`guideFor(${JSON.stringify(st)}, ${known?'{text:"岗位"}':'{text:""}'})`);
    assert.deepEqual([g.ob, g.news], [ob, news], JSON.stringify(st)+known);
  }
  assert.match(evaluate("newsView()"),/重新走一遍/); evaluate("S.ob=null; S.news=false");
  const last = evaluate('D.settings.drivers=[]; D.settings.engine="codex"; S.ob={step:OB.length-1}; obView()');
  for (const label of ["等你把那段话发给 AI","先进去用","copySetupPrompt()","大约还要"]) assert.ok(last.includes(label), label);
  assert.doesNotMatch(last,/本机 GPT|本机 Claude|测试连接/, "the newcomer does not choose an engine");
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
  evaluate(`D.inbox=${JSON.stringify([brief,oldCard,oldText])}; S.view="inbox"; S.inboxTab=""; S.inboxOld=true;`);
  let page=evaluate("inboxPage()");
  assert.match(page,/onclick="openMsg\(7\)"/,"a brief about one item still opens as a brief");
  assert.doesNotMatch(page,/今天你要做/,"closed until clicked");
  evaluate("openMsg(7)");
  assert.equal(evaluate("D.inbox[0].read"),true);
  page=evaluate("S.inboxOld=false; inboxPage()");
  for (const words of ["今天的会 · 1 场","10:00  周会","今天你要做 · 1 件","AI 做好了，等你看 · 1 件","需要你定 · 1 件","还有 2 件","收起"]) assert.ok(page.includes(words),words);
  assert.match(page,/onclick="openItem\(26\)"/);
  assert.match(page,/<span class="pill amber">待验收<\/span>/);
  assert.match(page,/act\(26,'approve'/,"same buttons as a list row");
  assert.match(page,/act\(24,'me'/); assert.match(page,/act\(24,'ai'/);
  assert.match(page,/act\(9,'done'/,"your own open item can be finished from the brief");
  assert.match(page,/href="https:\/\/x\.feishu\.cn\/docx\/a"[^>]*>打开文档/);
  assert.match(page,/href="https:\/\/vc\.feishu\.cn\/j\/1"[^>]*>进入会议 ↗/);
  assert.equal((page.match(/1 场会 · 你要做 1 件/g)||[]).length,1,"the first line is not repeated");
  await evaluate('apply(26,"approve",null)');               // a click in the brief acts like the list row
  page=evaluate("S.inboxOld=false; inboxPage()");
  assert.match(page,/<span class="pill green">已完成<\/span>/);
  assert.doesNotMatch(page,/act\(26,'approve'/);
  // The answer box opens in the brief it was clicked in, not on another row of the same item.
  evaluate('document.querySelector("#x").focus=()=>{}');
  evaluate('act(31,"answer","直接回答 AI 的问题",{closest:()=>({dataset:{msg:"6"}})})');
  assert.equal(evaluate("S.inline.at"),"6");
  assert.doesNotMatch(evaluate("inboxPage()"),/inline-31/,"brief 6 is closed");
  evaluate("openMsg(6)");
  page=evaluate("S.inboxOld=false; inboxPage()");
  assert.equal((page.match(/id="inline-31"/g)||[]).length,1);
  assert.doesNotMatch(page,/回「|回复「|退 要改的地方/,"old reply instructions are dropped, the whole footer too");
  assert.match(page,/什么时候做完？/); assert.match(page,/AI 卡住：缺收入导出/);
  assert.match(page,/act\(31,'answer'/);
  evaluate("S.inline=null; openMsg(5)");
  page=evaluate("S.inboxOld=false; inboxPage()");
  assert.match(page,/<div class="b-h">你要做<\/div>/);
  assert.match(page,/onclick="openItem\(9\)"/); assert.match(page,/逾期1天/);
  assert.doesNotMatch(page,/回复：/);
  assert.match(page,/>看板 ↗/);
  // An item shows the line it is on, offers likely relatives, and lets the owner replace the deliverable link.
  evaluate(`D.items=[{n:1,title:"标签调研",status:"完成",who:"AI",created:"2026-11-10",after:[2],before:[],related:[]},
    {n:2,title:"标签需求文档",status:"待验收",who:"AI",created:"2026-11-12",before:[1],after:[],related:[3],url:"https://x/docx/OLD"},
    {n:3,title:"标签上线排期",status:"未开始",who:"我",created:"2026-11-14",before:[],after:[],related:[2]}]; S.item=2; S.linking=null;`);
  assert.match(evaluate("itemView(byN(2))"),/关联 1 件/, "the line it is on is one tap away");
  evaluate("S.more=2"); let view=evaluate("itemView(byN(2))");
  assert.match(view,/前面的事[\s\S]*#1 标签调研/); assert.match(view,/可能相关[\s\S]*#3 标签上线排期[\s\S]*串起来/); assert.match(view,/换交付链接/);
  evaluate("S.more=3"); assert.match(evaluate("itemView(byN(3))"),/填交付链接/); evaluate("S.more=null");
  await evaluate("linkByAge(2,3)");
  assert.deepEqual(plain("[byN(2).after,byN(3).before]"),[[3],[2]],"the item created first is the earlier step");
  await evaluate("setLink(1,2,false)");
  assert.deepEqual(plain("[byN(1).after,byN(2).before]"),[[],[]]);
  assert.equal(evaluate('CMD.link(2,"https://x/docx/NEW")'),"2 交付链接 https://x/docx/NEW");
  // Every path is the owner's choice: what is taken in, which tools are read, where briefs go; and items can be added by hand.
  evaluate('D.settings={intake_mode:"accepted",sources:{meetings:true,chats:false,tasks:true},brief_channel:"feishu"}; D.system={record:{feishu_ready:true}}; S.setTab="tool"');
  let src=evaluate("sysPage()")+pane("mode");
  for (const label of ["全部接","答应了才接","不自动接","读会议","读聊天","读飞书任务","Slack"]) assert.ok(src.includes(label), label);
  assert.match(src,/class="mode on" aria-pressed="true" onclick="setSetting\('intake_mode','accepted'\)"/);
  assert.match(pane("notify"),/早晚简报也发到飞书/);
  evaluate('D.system={record:{feishu_ready:false}}');   // nothing connected: no Feishu switches, notifications stay in the app
  assert.match(pane("tool"),/setupAsk\('feishu'\)/); assert.doesNotMatch(pane("tool"),/读会议/);
  assert.match(pane("notify"),/所有通知都只在心响里/);
  await evaluate('setSetting("intake_mode","all")');
  assert.deepEqual(plain("[D.settings.intake_mode,D.settings.auto_assign,D.settings.prework_enabled]"),["all",true,true]);
  evaluate("addOpen()"); assert.match(evaluate("addView()"),/要做什么[\s\S]*谁来做[\s\S]*创建/); evaluate("S.adding=null");
  // The inbox sorts by what the owner has to do, and says it in plain words.
  evaluate(`TODAY="2026-11-17"; D.items=[{n:65,title:"标签更新",status:"待验收",who:"AI"},{n:66,title:"付费归因",status:"完成",who:"AI"},{n:74,title:"加到总表",status:"未开始",who:"AI"}];
    D.inbox=[{id:1,at:"2026-11-17T09:00:00",kind:"done",title:"AI 做完了，请你验收",refs:[65],read:true,blocks:[]},
             {id:2,at:"2026-11-17T09:01:00",kind:"done",title:"AI 做完了，请你验收",refs:[66],read:true,blocks:[]},
             {id:3,at:"2026-11-17T09:02:00",kind:"new",title:"聊天 · 新增 1 件：AI #74",text:"回复：展开 74 看原话",refs:[74],read:true,blocks:[]},
             {id:4,at:"2026-11-16T12:29:00",kind:"prep",title:"13:00 复盘会",text:"30 分钟后开始",refs:[],read:true,blocks:[]},
             {id:5,at:"2026-11-17T12:46:00",kind:"system",title:"⚠ 会议「例会」有内容被校验拦掉了，可能漏了待办",text:"去掉 1 条",refs:[],read:true,blocks:[]}]; S.inboxTab="";`);
  assert.deepEqual(plain("D.inbox.map(inboxBucket)"), ["need","today","today","old","today"]);
  assert.equal(evaluate('inboxBucket({kind:"blocked",at:"2026-11-15T09:00:00",title:"AI 卡住，需要你补材料",refs:[],read:true})'),"old","an old question about an item that is gone asks nothing of the owner");
  page=evaluate("S.inboxOld=false; inboxPage()");
  assert.match(page,/要你处理[\s\S]*#65 标签更新[\s\S]*今天[\s\S]*#74 加到总表/);
  assert.match(page,/来自聊天/); assert.match(page,/查看更早的 1 条/);
  assert.doesNotMatch(page,/和录音对不上/, "the system's own notices are not in the owner's messages");
  assert.match(evaluate('S.inboxTab="system"; inboxPage()'),/和录音对不上，没有采用/); evaluate('S.inboxTab=""');
  assert.doesNotMatch(page,/校验拦掉|展开 74 看原话|13:00 复盘会/);
  assert.match(evaluate('S.inboxTab=""; S.inboxOld=true; inboxPage()'),/更早[\s\S]*13:00 复盘会/);
  // 交给 AI 的说明: the item detail shows the words the AI got, as they were
  evaluate('D.items=[{n:61,rid:"rec61",title:"写需求",status:"待验收",who:"AI"}]; PROMPTS.rec61={text:"## 共同规矩\\n读者没参加那场会",at:"11-26 15:00"}; S.tab="prompt"');
  const sent = evaluate("itemView(byN(61))");
  assert.match(sent, /AI 收到的说明/); assert.match(sent, /class="prompt-text">## 共同规矩/); assert.match(sent, /11-26 15:00 交给 AI 的完整说明/);
  evaluate('PROMPTS.rec61={text:"",at:""}'); assert.match(evaluate("itemView(byN(61))"), /还没交给过 AI/);
  evaluate('S.tab="talk"');
  // 做事方式: one switch on 事项 for everything, one in the item for that item; the outline and the delivery are shown plainly
  evaluate(`D.settings={...(D.settings||{}),work_mode:"auto",specs:[{kind:"需求文档",state:"trial",label:"试用中",uses:3,passed:2,recent:3,reviewed:"",why:"",text:"## 结构\\n1. 背景",proposal:"## 结构\\n1. 背景\\n2. 验收"},{kind:"周报",state:"none",label:"还没有",uses:0,passed:0,recent:0,reviewed:"",why:"",text:"",proposal:""}]};
    D.items=[{n:71,rid:"rec71",title:"写切换模型需求",status:"卡住",who:"AI",blocker:"提纲好了：按这个提纲写吗？",work:{kind:"需求文档",spec:"试用中",mode:"auto",own_mode:"",outline:"## 提纲\\n1. 要解决什么问题",waiting_outline:true,review:[],fixed:0}},
             {n:72,rid:"rec72",title:"写周报",status:"待验收",who:"AI",work:{kind:"周报",spec:"草稿",mode:"careful",own_mode:"careful",outline:"",waiting_outline:false,review:["✓ 有结果","改了：补了风险"],fixed:1}}];
    D.deliverables={72:[{v:1,at:"11-26 18:00",summary:"本周周报",url:"https://x.feishu.cn/docx/a",confirm:"下周计划要不要加评审"}]}; D.inbox=[]; S.view="all"; S.all="全部"; S.dim="type"`);
  const bar = evaluate("allPage()");
  for (const label of ["AI 做事","快速","平衡","仔细","setGlobalMode","直接开工 · 新类型先给提纲 · 新类型多审一轮"]) assert.ok(bar.includes(label), label);
  assert.doesNotMatch(bar, /<select aria-label="做事方式"/, "no dropdown: the choices are on the page, with what the current one means");
  assert.match(bar, /class="on" role="radio" aria-checked="true"[^>]*>平衡</);
  // a mix of the three choices that is none of the ready-made ones is 自定义, and says what it is
  evaluate('D.settings.work_style={start:"ask",outline:"new",review:"list"}');
  const mixed = evaluate("allPage()");
  assert.match(mixed, /aria-checked="true"[^>]*>自定义</); assert.match(mixed, /先问我 · 新类型先给提纲 · 对照清单自查/);
  const work = evaluate('S.setTab="work"; sysPage()');
  for (const label of ["搭好的三种","逐项选","开工","写之前","写完后","直接开工","先问我","不给提纲","每件先给提纲","对照清单自查","每件多审一轮","自定义","setStyle('start','auto')","会议里提的事"]) assert.ok(work.includes(label), label);
  assert.match(work, /class="ws-opt on" aria-pressed="true" onclick="setStyle\('start','ask'\)"/);
  await evaluate("setStyle('outline','never')"); assert.equal(evaluate("wsNow().preset"), "custom");
  await evaluate("setGlobalMode('careful')"); assert.deepEqual(plain("[wsNow().preset,wsNow().start,wsNow().outline,wsNow().review]"), ["careful","ask","always","always"]);
  await evaluate("setGlobalMode('auto')");
  evaluate("S.more=71"); const outline = evaluate("itemView(byN(71))");
  for (const label of ["提纲：等你看过再写","按提纲写","修改提纲…","要解决什么问题","类型：需求文档 · 规格：试用中"]) assert.ok(outline.includes(label), label);
  assert.doesNotMatch(outline, /按现有资料继续/, "an outline waits for 按提纲写, not a generic continue");
  assert.deepEqual(plain("rowButtons(byN(71)).map(b=>b[0])"), ["按提纲写"]);
  evaluate("S.more=72"); const done = evaluate("itemView(byN(72))");
  for (const label of ["周报","规格：草稿","做事方式：仔细","v1","本周周报","待你确认","下周计划要不要加评审","自审 2 项，改了 1 处","打开文档 ↗","验收通过","让 AI 修改"]) assert.ok(done.includes(label), label);
  assert.ok(done.indexOf("打开文档 ↗") < done.indexOf(">验收通过<") && done.indexOf(">验收通过<") < done.indexOf('class="tabs"'), "the delivery and what to do with it are one card, above the conversation");
  evaluate("S.more=null"); const plainView = evaluate("itemView(byN(72))");
  assert.doesNotMatch(plainView, /做事方式<\/span>|跟着你的|哪个 AI/, "the rarely used settings stay in 更多");
  assert.match(plainView, />更多</);
  assert.equal((done.match(/>验收通过</g)||[]).length, 1, "deciding on it is in one place");
  assert.match(done, /aria-checked="true" onclick="event.stopPropagation\(\);setWorkMode\('careful'/, "this item's own mode is shown as chosen");
  assert.match(done, /先问我 · 每件先给提纲 · 每件多审一轮/, "and what it means");
  evaluate("S.more=71"); assert.match(evaluate("itemView(byN(71))"), /跟着你的/); evaluate("S.more=null");
  assert.equal((outline.match(/按提纲写/g)||[]).length, 1, "one place for each button");
  assert.match(evaluate('row({n:99,title:"交付",status:"待验收",who:"AI",url:"https://x.test/d"})'), /class="doc-link" href="https:\/\/x.test\/d"[^>]*>文档 ↗</, "the deliverable opens straight from the row");
  assert.doesNotMatch(evaluate('row({n:98,title:"无",status:"未开始",who:"我"})'), /doc-link/);
  assert.doesNotMatch(outline, /## 提纲/, "the outline reads as headings, not as # marks");
  const specCards = evaluate("specsView()");
  for (const label of ["交付规格","需求文档","试用中","最近 3 次里一次通过 2 次","有一份修订等你确认","周报","还没有","第一次做这一类事时"]) assert.ok(specCards.includes(label), label);
  // 术语：状态、分区、按钮只用 TERM 里的说法；这些口语和旧叫法在页面上一处都不许出现
  const BANNED = /卡住|要你来|看起来|都不要|都我来|我自己来|办完|AI 问你|等你定|你来做|别人在做|AI 排队|AI 正在做|等你验收|关掉|没办完|恢复（标错了）|先做这件|要你看|等你看|等你确认接不接|AI 问：/;
  evaluate(`D.items=[{n:1,title:"甲",status:"待确认",who:"我"},{n:2,title:"乙",status:"未开始",who:"我",due:TODAY},{n:3,title:"丙",status:"进行中",who:"AI"},
    {n:4,title:"丁",status:"卡住",who:"AI",blocker:"口径"},{n:5,title:"戊",status:"卡住",who:"我",blocker:"等合同"},{n:6,title:"己",status:"待验收",who:"AI"},
    {n:7,title:"庚",status:"完成",who:"我"},{n:8,title:"辛",status:"取消",who:"别人"},{n:9,title:"壬",status:"进行中",who:"别人"},
    {n:10,title:"癸",status:"未开始",who:"我",done_hint:{evidence:"已发",where:"群"}}]; D.inbox=[]; D.judgments={1:{decision:"prep",reason:"要拍板"}}; S.view="all"; S.dim="type"`);
  for (const all of ["今天","全部","已结束"]) {
    const page = evaluate(`S.all="${all}"; allPage()`); const hit = page.match(BANNED);
    assert.ok(!hit, `事项·${all} 里出现了「${hit&&hit[0]}」`);
  }
  for (let n = 1; n <= 10; n++) { const view = evaluate(`itemView(byN(${n}))`); const hit = view.match(BANNED); assert.ok(!hit, `#${n} 的详情里出现了「${hit&&hit[0]}」`); }
  assert.deepEqual(plain("D.items.map(i=>statusOf(i)[0])"), ["待分配","未开始","进行中","待答复","受阻","待验收","已完成","已取消","进行中","未开始"]);
  const labels = plain("D.items.flatMap(i=>[...rowButtons(i),...detailButtons(i)].map(b=>b[0]))"), acts = plain("Object.values(TERM.act)");
  for (const l of labels) assert.ok(acts.includes(l), `按钮「${l}」不在 TERM.act 里：新按钮先在术语表里起名`);
  for (const html of [evaluate("healthDot(1)"), evaluate("healthDot(0)"), evaluate("sideHTML()")]) assert.doesNotMatch(html, BANNED);
  console.log("Client checks passed: new, running, cancel, restore, complete, closed-item conversation, stale status, concurrency copy, briefs opened in the inbox.");
})().catch(e => { console.error(e); process.exitCode=1; });
