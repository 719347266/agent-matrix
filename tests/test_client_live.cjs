// Live request state, using mocked transport; no real tasks are changed.
const assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm"), path=require("node:path");
const raw=fs.readFileSync(path.join(__dirname,"../src/agent_matrix/app/xinxiang.html"),"utf8").match(/<script>([\s\S]*?)<\/script>/)[1];
const clone=x=>JSON.parse(JSON.stringify(x));
const base={today:"2026-11-17",items:[{n:1,title:"测试",status:"进行中",who:"我"}],threads:{},deliverables:{},inbox:[],worker:{},settings:{},judgments:{},commands:[]};
function harness(fetch,storage=new Map()){
  const elements=new Map();
  const document={documentElement:{dataset:{}},addEventListener(){},activeElement:null,querySelectorAll:()=>[],querySelector:s=>{
    if(!elements.has(s))elements.set(s,{innerHTML:"",value:"",dataset:{},classList:{add(){},remove(){},toggle(){}},textContent:""});
    return elements.get(s);
  }};
  const context=vm.createContext({console,Date,AbortController,fetch,document,
    sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)}, location:{hash:""},window:{innerWidth:1440,scrollTo(){},addEventListener(){}},
    setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){}});
  vm.runInContext(raw.replace("/*__DATA__*/null",JSON.stringify(base)).replace("const LIVE = !D;","const LIVE = true;")
    .replace("if(LIVE){ load(); } else render();","render();"),context);
  return {run:s=>vm.runInContext(s,context),elements,storage};
}
const response=data=>({ok:true,status:200,json:async()=>clone(data)});
(async()=>{
  let remote=clone(base), posts=[], resolvePost;
  const h=harness(async(url,opts)=>{
    if(url==="/api/state")return response(remote);
    posts.push(JSON.parse(opts.body));
    return new Promise(resolve=>resolvePost=resolve);
  });
  const first=h.run('apply(1,"done",null)');
  await h.run('apply(1,"done",null)');
  assert.equal(posts.length,1,"double click sends only one request");
  assert.equal(h.run('byN(1).status'),"进行中","no optimistic success");
  assert.match(h.run('btn(byN(1),["完成","done"])'),/disabled/);
  assert.match(h.elements.get("#actions").innerHTML,/正在提交/);
  const command={id:posts[0].request_id,numbers:[1],text:"1 完成",status:"running",message:"正在核对保存结果",created:Date.now()/1000};
  remote.commands=[command];
  resolvePost(response({command}));
  await first;
  assert.match(h.elements.get("#actions").innerHTML,/核对保存结果/);
  assert.equal(h.run('pendingFor(1).id'),command.id);
  remote.items[0].status="完成";remote.commands[0]={...command,status:"done",message:"#1 完成 ✓"};
  await h.run("load()");
  assert.equal(h.run("byN(1).status"),"完成");
  assert.equal(h.run("pendingFor(1)"),undefined);
  assert.match(h.elements.get("#actions").innerHTML,/完成 ✓/);

  // POST response lost after acceptance: GET reconciles it, and the item stays locked.
  remote=clone(base);posts=[];
  const loss=harness(async(url,opts)=>{
    if(url==="/api/state")return response(remote);
    const body=JSON.parse(opts.body);posts.push(body);
    remote.commands=[{...command,id:body.request_id}];
    throw new Error("connection lost");
  });
  await loss.run('apply(1,"done",null)');
  await loss.run('apply(1,"done",null)');
  assert.equal(posts.length,1);
  assert.equal(loss.run('pendingFor(1).status'),"running");

  // No acknowledgement at all: reload retains uncertainty; explicit retry uses the SAME ID.
  remote=clone(base);posts=[];const storage=new Map();
  let connected=false;
  const fetch=async(url,opts)=>{
    if(url==="/api/state")return response(remote);
    const body=JSON.parse(opts.body);posts.push(body);
    if(!connected)throw new Error("offline");
    remote.commands=[{...command,id:body.request_id}];
    return response({command:remote.commands[0]});
  };
  const lost=harness(fetch,storage);
  await lost.run('apply(1,"done",null)');
  assert.equal(lost.run('pendingFor(1).status'),"unknown");
  const reloaded=harness(fetch,storage);
  assert.equal(reloaded.run('pendingFor(1).status'),"unknown");
  connected=true;
  await reloaded.run('retryCommand(pendingFor(1).id)');
  assert.equal(posts.length,2);
  assert.equal(posts[0].request_id,posts[1].request_id);

  // A delayed older /state response must not resurrect the running status.
  let oldResolve, gets=0;
  const old=clone(base),fresh=clone(base);fresh.items[0].status="完成";
  const race=harness(async()=>++gets===1?new Promise(r=>oldResolve=r):response(fresh));
  const oldLoad=race.run("load()");
  await race.run("load()");
  oldResolve(response(old));await oldLoad;
  assert.equal(race.run('byN(1).status'),"完成");

  // macOS can leave a textarea focused when a navigation button is clicked.
  const navigation=harness(async()=>response(clone(base)));
  navigation.run('openItem(1); document.activeElement={tagName:"TEXTAREA",value:"尚未发送",dataset:{draft:"say-1"},closest:()=>true}; S.view="all"; render();');
  assert.match(navigation.elements.get("#ttl").innerHTML,/全部事项/);
  let writes=0, markup=navigation.elements.get("#main").innerHTML;
  Object.defineProperty(navigation.elements.get("#main"),"innerHTML",{get:()=>markup,set:v=>{writes++;markup=v}});
  navigation.run('for(let i=0;i<20;i++)render()');
  assert.equal(writes,0,"unchanged polling must not replace the page DOM");

  const live=harness(async()=>response(clone(base)));
  live.run('D.items[0].who="AI"; D.threads[1]=[{from:"me",kind:"feedback",text:"改一下"}]; D.worker.queue=[2,1]');
  assert.match(live.run('replyState(byN(1))'), /已排队，前面有 1 件/);
  live.run('D.worker.doing=[{n:1,kind:"item",driver:"Claude",step:"2/4 阅读资料"}]; D.threads[1].push({from:"ai",kind:"step",text:"阅读资料"});');
  assert.match(live.run('replyState(byN(1))'), /Claude 已接手：阅读资料/);
  live.run('D.threads[1].push({from:"ai",kind:"say",text:"做好了"})');
  assert.equal(live.run('replyState(byN(1))'),"");
  live.run('D.running=[{job:"one",n:1,engine:"other",driver:"Cindy",can_open:true,step:"阅读"}]');
  assert.match(live.run('sideHTML()'),/Cindy/);
  assert.match(live.run('sideHTML()'),/打开/);
  live.run('D.running=[]; D.local_sessions=[{key:"cindy:one",job:"local:cindy:one",driver:"Cindy",local:true,title:"本机会话",can_open:true,status:"open"}]');
  assert.match(live.run('sideHTML()'),/未关联事项/);
  live.run('S.bindKey="cindy:one"');
  assert.match(live.run('sideHTML()'),/选择事项/);
  live.run('D.local_sessions[0].n=1');
  assert.match(live.run('sideHTML()'),/已关联事项/);
  live.run('byN(1).reply_timing={sent_at:"2026-11-17T17:37:00+08:00",pickup_at:"2026-11-17T17:46:00+08:00",reply_at:"2026-11-17T17:47:00+08:00"}; D.reply_timing_summary={days:30,replies:1,pickups:1,median_reply_seconds:600,median_pickup_seconds:540}');
  assert.match(live.run('replyState(byN(1))'),/从留言到首次回复或交付：10 分钟/);
  assert.match(live.run('replyState(byN(1))'),/首次处理记录距留言 9 分钟/);
  live.run('byN(1).reply_timing.reply_at=null; D.threads[1]=[{from:"me",kind:"feedback",text:"再改一下"}]');
  assert.match(live.run('replyState(byN(1))'),/留言已等/);
  live.run('byN(1).who="我"');
  assert.doesNotMatch(live.run('replyState(byN(1))'),/留言已等/);
  console.log("Live client checks passed: immediate feedback, double click, verified success, lost acknowledgement, reload, safe retry, stale response.");
})().catch(e=>{console.error(e);process.exitCode=1});
