"""Validate a model's grounded review, then publish to the owner's existing Base."""
import argparse
from datetime import date, timedelta
from hashlib import sha256, sha1
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from . import db as database, memory
from .bitable import find_value, items, js, require_owned
from .config import load
from .feishu import Feishu
from .meeting_watch import MeetingQueue, parse_time
from .models import MatrixError
from .state import State, exclusive_lock


class Grounded(BaseModel):
    model_config=ConfigDict(extra="forbid")
    text: str = Field(min_length=1,max_length=1000)
    quote: str = Field(min_length=1,max_length=2000)
    timestamp: str = Field(pattern=r"^\d{1,2}:\d{2}(?::\d{2})?$" )
    reference: str = Field(min_length=1,max_length=6000)


class Action(Grounded):
    title: str = Field(min_length=1,max_length=60)
    owner_name: str | None = None
    due_date: str | None = None
    due_phrase: str | None = None
    direction: Literal["mine","others_to_me","others","unknown"] = "unknown"
    # The exact words in which somebody takes on or assigns the work ("我来", "你负责").
    commitment_phrase: str = Field(min_length=1,max_length=40)
    confidence: float = Field(ge=0,le=1)
    executor: Literal["我","AI","别人"] | None = None
    ai_can_prep: bool = False
    done_when: str | None = Field(default=None,max_length=60)
    # Number of an open item this is the same thing as (see open_items.json); merged instead of re-created.
    same_as: int | None = None
    # Number of an item this one continues (the next step after it); see related_items.json. Not the same thing.
    follows: int | None = None


class Waiting(Grounded):
    """Left open on purpose until a condition is met: "先保留，等周五数据出来再定"."""
    wait_for: str = Field(min_length=1,max_length=60)      # what it waits for, in plain words
    due_date: str | None = None                            # only when the words give a date
    due_phrase: str | None = None


MIN_CONFIDENCE=0.8


class Review(BaseModel):
    model_config=ConfigDict(extra="forbid")
    meeting_id: str
    transcript_sha256: str
    decisions: list[Grounded] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    requirements: list[Grounded] = Field(default_factory=list)
    open_questions: list[Grounded] = Field(default_factory=list)
    waiting: list[Waiting] = Field(default_factory=list)
    # Accepted for older files only; never stored, shown or turned into tasks.
    suggestions: list[str] = Field(default_factory=list,exclude=True)


def kept_actions(review):
    """Only clear, confident commitments become tasks; an empty list is a normal result."""
    return [a for a in review.actions if a.confidence>=MIN_CONFIDENCE]


def resolve_due(phrase, meeting_date):
    if phrase in {"今天","当天","明天","后天"}:
        return (meeting_date+timedelta(days={"今天":0,"当天":0,"明天":1,"后天":2}[phrase])).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}",phrase):
        return date.fromisoformat(phrase).isoformat()
    m=re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]?",phrase)
    if m:
        return date(int(m[1]) if m[1] else meeting_date.year,int(m[2]),int(m[3])).isoformat()
    m=re.fullmatch(r"(本周|这周|下周)([一二三四五六日天])",phrase)
    if m:
        weekday="一二三四五六日".index(m[2].replace("天","日"))
        return (meeting_date-timedelta(days=meeting_date.weekday())+timedelta(days=weekday+(7 if m[1]=="下周" else 0))).isoformat()
    raise MatrixError("截止日表达存在歧义，请保留空值并列为待确认。")


CITE=re.compile(r'<cite[^>]*user-name="([^"]+)"[^>]*></cite>')


def speaker_of(transcript, quote):
    """Name on the last speaker label before the quote, or None when the transcript has no labels."""
    i=transcript.find(quote)
    if i<0:
        return None
    found=list(CITE.finditer(transcript[:i]))
    if not found:
        return None
    last=found[-1]
    # "说话人 1 用 <cite 王小明> 的设备": somebody else speaking on the owner's device.
    line_start=transcript.rfind("\n",0,last.start())+1
    if "说话人" in transcript[line_start:last.start()]:
        return "（借用设备的未知说话人）"
    return last.group(1)


def aliases(name, extra=()):
    """Ways colleagues address someone: 李大伟 → 大伟 / 小李; plus configured nicknames."""
    out={name,*extra}
    if len(name)>=3:
        out|={name[1:],"小"+name[0],"老"+name[0]}
    return {a for a in out if len(a)>=2}


def owner_is_grounded(action, transcript, extra_aliases=()):
    """The owner either says the line themself or is named in it; a bare "你" names nobody."""
    speaker=speaker_of(transcript,action.quote)
    if speaker is None:
        return action.owner_name in action.reference
    if action.owner_name==speaker:
        return True
    spoken=CITE.sub("",action.reference)
    return any(a in spoken for a in aliases(action.owner_name,extra_aliases))


def validate_review(review, transcript, meta, owner_aliases=()):
    if review.meeting_id!=meta["meeting_id"] or sha256(transcript.encode()).hexdigest()!=review.transcript_sha256:
        raise MatrixError("提取结果的会议或逐字稿指纹不一致。")
    seen=set()
    for entry in [*review.decisions,*review.actions,*review.requirements,*review.open_questions,*review.waiting]:
        if entry.reference not in transcript or entry.quote not in entry.reference or entry.timestamp not in entry.reference:
            raise MatrixError("原话或时间戳未在对应原文片段中找到，拒绝写入。")
    for action in review.actions:
        if action.commitment_phrase not in action.reference:
            raise MatrixError("待办缺少原话里的承接或指派字眼（例如\u201c我来\u201d\u201c你负责\u201d），不算待办。")
        if action.owner_name and not owner_is_grounded(action,transcript,owner_aliases if action.owner_name==meta.get("owner_name") else ()):
            raise MatrixError(f"「{action.title}」的负责人{action.owner_name}既不是说这句话的人，原话里也没点名；负责人改为 null。")
        if action.due_date:
            if not action.due_phrase or action.due_phrase not in action.reference:
                raise MatrixError("截止日缺少对应原话，不能猜填。")
            expected=resolve_due(action.due_phrase,parse_time(meta["start_time"]).date())
            if action.due_date!=expected:
                raise MatrixError("截止日期与原话换算不一致。")
        key=re.sub(r"\s+","",action.title).casefold()
        if key in seen:
            raise MatrixError("提取结果含重复待办，请先合并。")
        seen.add(key)
    for w in review.waiting:
        if w.due_date and (not w.due_phrase or w.due_phrase not in w.reference
                           or w.due_date!=resolve_due(w.due_phrase,parse_time(meta["start_time"]).date())):
            raise MatrixError("等待条件的日期与原话不一致，不能猜填。")
    return review


def _loose(text):
    """A pattern for text that may differ from the source only in whitespace: the AI drops the two spaces
    a transcript puts before each line break, or joins two lines into one."""
    chars=re.sub(r"\s+","",str(text or ""))
    return re.compile(r"\s*".join(re.escape(c) for c in chars)) if chars else None


def _paragraph_with(source, quote, timestamp, limit=1500):
    """The AI's copy of the surrounding lines is off, but the quote itself is real: take the speaker's
    paragraph (header with the timestamp + what was said) straight from the transcript instead."""
    pattern=_loose(quote)
    for found in (pattern.finditer(source) if pattern else []):
        start=source.rfind("\n\n",0,found.start()); start=0 if start<0 else start+2
        end=source.find("\n\n",found.end()); end=len(source) if end<0 else end
        para=source[start:end].strip("\n")
        if timestamp and timestamp in para and len(para)<=limit:
            return para
    return None


def snap(entry, source):
    """Copy the exact source words back into an entry whose quote differs from them only in whitespace.
    Nothing else is forgiven: a reworded or shortened quote still fails the strict check afterwards."""
    updates={}
    ref=entry.reference
    if ref not in source:
        pattern=_loose(ref)
        found=pattern.search(source) if pattern else None
        ref=found.group(0) if found else _paragraph_with(source,entry.quote,entry.timestamp)
        if not ref:
            return entry
        updates["reference"]=ref
    for name in ("quote","commitment_phrase","due_phrase"):
        value=getattr(entry,name,None)
        if value and value not in ref:
            pattern=_loose(value)
            found=pattern.search(ref) if pattern else None
            if found:
                updates[name]=found.group(0)
    return entry.model_copy(update=updates) if updates else entry


def repair_review(review, transcript, meta, owner_aliases=()):
    """Fix what can be fixed entry by entry, so one bad line never sinks a whole meeting.

    The evidence rules stay strict (validate_review still runs after this): an entry whose quote is not in
    the transcript, or an action without a commitment phrase, is dropped; an owner the transcript does not
    support becomes "unknown" (待确认); a due date without matching words is cleared. Returns (review, notes)."""
    notes, seen = [], set()
    for name in ("decisions", "requirements", "open_questions", "waiting", "actions"):
        setattr(review, name, [snap(e, transcript) for e in getattr(review, name)])
    def grounded(entry):
        return entry.reference in transcript and entry.quote in entry.reference and entry.timestamp in entry.reference
    waits = []
    for w in review.waiting:
        if not grounded(w):
            notes.append(f"去掉一条原话对不上的等待条件"); continue
        if w.due_date:
            try:
                ok = bool(w.due_phrase and w.due_phrase in w.reference
                          and w.due_date == resolve_due(w.due_phrase, parse_time(meta["start_time"]).date()))
            except Exception:
                ok = False
            if not ok:
                w = w.model_copy(update={"due_date": None, "due_phrase": None})
        waits.append(w)
    review.waiting = waits
    for name in ("decisions", "requirements", "open_questions"):
        kept = [e for e in getattr(review, name) if grounded(e)]
        if len(kept) < len(getattr(review, name)):
            notes.append(f"去掉 {len(getattr(review, name)) - len(kept)} 条原话对不上的{ {'decisions': '结论', 'requirements': '需求', 'open_questions': '问题'}[name] }")
        setattr(review, name, kept)
    actions = []
    for a in review.actions:
        if not grounded(a):
            notes.append(f"去掉「{a.title}」：原话对不上"); continue
        if a.commitment_phrase not in a.reference:
            notes.append(f"去掉「{a.title}」：没有承接字眼"); continue
        if a.owner_name and not owner_is_grounded(a, transcript, owner_aliases if a.owner_name == meta.get("owner_name") else ()):
            # Not provably the owner's: it is someone else's business, kept hidden, never handed to the owner or the AI.
            notes.append(f"「{a.title}」负责人对不上（{a.owner_name}），当作别人的事，不列给你")
            a = a.model_copy(update={"owner_name": None, "direction": "others", "executor": "别人"})
        if a.due_date:
            ok = bool(a.due_phrase and a.due_phrase in a.reference)
            if ok:
                try:
                    ok = a.due_date == resolve_due(a.due_phrase, parse_time(meta["start_time"]).date())
                except Exception:
                    ok = False
            if not ok:
                notes.append(f"「{a.title}」截止日留空（原话里没有对应的日期说法）")
                a = a.model_copy(update={"due_date": None, "due_phrase": None})
        key = re.sub(r"\s+", "", a.title).casefold()
        if key in seen:
            notes.append(f"去掉重复的「{a.title}」"); continue
        seen.add(key)
        actions.append(a)
    review.actions = actions
    return review, notes


def task_fields(action, config, meta):
    # Other names remain in the evidence until a unique participant ID is resolved.
    own=bool(action.owner_name and config.owner_name.strip() and config.owner_open_id.strip()
             and action.owner_name==config.owner_name)
    if own:
        executor="AI" if action.executor=="AI" else "我"
    elif action.direction=="mine" and not action.owner_name:
        executor=action.executor if action.executor in {"我","AI"} else "我"  # the owner's, only the name was not said
    else:
        # Someone else's, or not provably the owner's: not the owner's business (hidden unless promised to the owner).
        executor="别人"
    hidden=executor=="别人" and action.direction!="others_to_me"
    ready=bool(action.due_date) and (own or executor=="别人")
    # A meeting is talk, not a handoff: what the AI would do from it waits for the owner's 「AI做」 (triage only suggests).
    status="未开始" if ready and executor!="AI" else "待确认"
    progress={"未开始":"0/1 等对方" if executor=="别人" else "0/1 未开始","待确认":"0/1 待确认"}[status]
    fields={"标题":action.title,"类型":["会议待办"],"状态":[status],"执行方":[executor],"进度":progress,
            "下一步动作":action.text,"来源":["会议"],"来源链接":meta.get("source_url") or None,
            "原话引用":f"[{action.timestamp}] {action.reference}",
            "去重键":"meeting:"+meta["meeting_id"]+":"+sha1(re.sub(r"\s+","",action.title).casefold().encode()).hexdigest()[:10]}
    if hidden:
        fields["排除本人提醒"]=True
    if action.done_when:
        fields["完成标准"]=action.done_when
    if own:
        fields["负责人"]=[{"id":config.owner_open_id}]
        if executor=="我" and action.ai_can_prep:
            fields["能否AI预做"]=True
    if action.due_date:
        fields["截止日"]=fields["首次截止日"]=action.due_date
    if action.direction!="unknown":
        fields["方向"]=[{"mine":"我的","others_to_me":"别人答应我的","others":"别人的"}[action.direction]]
    return fields


def summary_line(review, config, meta):
    """One line for the next brief; nothing at all when the meeting had no new tasks."""
    counts={"我":0,"AI":0,"别人":0}
    for action in kept_actions(review):
        fields=task_fields(action,config,meta)
        if not fields.get("排除本人提醒"):
            counts[fields["执行方"][0]]+=1
    total=sum(counts.values())
    if not total:
        return None
    parts=" / ".join(f"{'你' if k=='我' else k} {v}" for k,v in counts.items() if v)
    return f"{meta.get('topic') or '会议'} · 新增 {total} 件（{parts}）"


def render_review(review, config, meta):
    return summary_line(review,config,meta) or f"{meta.get('topic') or '会议'} · 无新待办"


def _open_rows(client, state):
    """Current items for same-thing checks; (None, None) when the table cannot be read (e.g. offline tests)."""
    try:
        from .workflow import Gateway
        gateway=Gateway(client,state)
        return gateway,gateway.rows()
    except Exception:
        return None,None


def _same_target(rows, action):
    from .merge import find_same, open_items, similarity
    if action.same_as is not None:
        target=next((r for r in open_items(rows) if str(r.get("编号")).strip()==str(action.same_as)),None)
        if target is None:
            raise MatrixError(f"「{action.title}」的 same_as #{action.same_as} 不是一件还没完成的事；去掉 same_as 或改成正确编号。")
        if similarity(action.title,target.get("标题"))<0.2 and not set(action.title)&set(str(target.get("标题"))):
            raise MatrixError(f"「{action.title}」和 #{action.same_as} 看不出是同一件事；确认后再填 same_as。")
        return target
    target,_,_=find_same(rows,action.title)
    return target


def publish(config, client, state, review):
    if not config.transcripts_to_llm_allowed or config.llm_backend!="cindy_current":
        raise MatrixError("会议处理权限尚未开启，不发布复盘。")
    require_owned(config,state)
    queue=MeetingQueue(state)
    row=queue.get(review.meeting_id)
    if not row or row["status"] not in {"prepared","done"}:
        raise MatrixError("会议未完成逐字稿准备，不能发布。")
    transcript=(client.root/row["transcript_path"]).read_text()
    validate_review(review,transcript,{**row["metadata"],"owner_name":config.owner_name},config.owner_aliases)
    prefix="meeting-review:"+review.meeting_id
    # Freeze the validated extraction before any writes. Changes require a new review.
    canonical=js(review.model_dump())
    old=state.get(prefix+":validated")
    if old is not None and old!=canonical:
        raise MatrixError("该会议已有确定的提取版本，不自动替换后重复建事项。")
    state.set(prefix+":validated",canonical)
    coords=["--base-token",config.base_token]
    from .itemstore import feishu_free
    local=feishu_free(config)   # items kept on this computer: no Feishu table to check or record the meeting in
    for table,required in [] if local else (("事项表",["标题","状态","原话引用","去重键"]),("会议处理记录表",["会议ID","提取状态","提取待办数"])):
        fields=items(client.call("+field-list",*coords,"--table-id",config.tables[table]))
        names={f.get("name",f.get("field_name")) for f in fields}
        if not set(required)<=names:
            raise MatrixError("复盘需要的字段已变化，停止写入："+table)
    task_ids=[]; merged=[]
    gateway,rows=_open_rows(client,state)
    for action in kept_actions(review):
        fields=task_fields(action,config,row["metadata"])
        target=_same_target(rows,action) if rows is not None else None
        if target is not None:
            # One thing, one item: the meeting's new words go into the existing item.
            from .merge import merge_into
            from datetime import datetime
            from .timeutil import TZ
            merge_into(gateway,target,title=action.title,source="会议「"+str(row["metadata"].get("topic") or "")[:12]+"」",
                       detail=f"[{action.timestamp}] {action.quote}",key=prefix+":merge:"+fields["去重键"],now=datetime.now(TZ))
            merged.append(target.get("编号"))
            continue
        handed=(getattr(config,"intake_mode","all")=="accepted" and fields["执行方"][0]!="别人"
                and speaker_of(transcript,action.quote) not in (None,config.owner_name))
        if handed:  # 全自动: someone else gave it to the owner in the meeting; it waits for the owner's yes
            fields.update({"状态":["待确认"],"进度":"0/1 待确认"})
        receipt=state.once(prefix+":"+fields["去重键"],lambda f=fields:client.call("+record-batch-create",*coords,
                           "--table-id",config.tables["事项表"],"--json",js({"create_records":[f]}),write=True))
        ids=receipt.get("record_id_list")
        if not isinstance(ids,list) or len(ids)!=1:
            raise MatrixError("事项创建回执缺少唯一记录 ID，停止。")
        task_ids.extend(ids)
        if handed:
            from .thread import judge
            judge(client.root,ids[0],"ask","他人在会上交给你")
        if rows is not None:
            from . import itemlinks
            itemlinks.link_by_number(client.root, rows, ids[0], action.follows)
    meta=row["metadata"]
    record={"会议ID":review.meeting_id,"标题":meta.get("topic"),"开始时间":parse_time(meta["start_time"]).isoformat(),
            "结束时间":parse_time(meta["end_time"]).isoformat(),"逐字稿状态":["已获取"],"提取状态":["已完成"],"提取待办数":len(task_ids)}
    if not local:
        state.once(prefix+":meeting-record",lambda:client.call("+record-batch-create",*coords,"--table-id",config.tables["会议处理记录表"],
                                                             "--json",js({"create_records":[record]}),write=True))
    # Owner wants control: right after the meeting, one line with the new numbers.
    # A meeting with no new tasks sends nothing.
    try:
        line=numbered_line(client,state,review,config,meta,task_ids)
    except (MatrixError,AttributeError):
        line=summary_line(review,config,meta)  # numbers unreadable right now: counts only
    if merged:
        joined="、".join(f"#{n}" for n in merged)
        line=(line+"\n" if line else f"{meta.get('topic') or '会议'} · ")+f"另有 {len(merged)} 件是已有的事，已并入 {joined}"
    if line:
        key="review-"+sha256(review.meeting_id.encode()).hexdigest()[:32]
        receipt=state.once(prefix+":notify",lambda:client.send_self(line,key))
    else:
        receipt={"notice":None,"task_ids":task_ids}
    queue.done(review.meeting_id,receipt)
    from . import activity
    activity.log(client.root,"detect",f"会议「{meta.get('topic') or '会议'}」提取完成："+(f"新增 {len(task_ids)} 件" if task_ids else "没有新待办"))
    dropped=[n for n in (state.get("meeting-repairs:"+review.meeting_id) or []) if n.startswith("去掉")]
    if dropped:  # never lose what the AI found without saying so
        from . import inbox
        inbox.add(client.root,"meeting-dropped:"+review.meeting_id,
                  f"会议「{meta.get('topic') or '会议'}」有几条内容和录音对不上，没有采用\n"+"\n".join(dropped[:8])
                  +"\nAI 整理出的这几条，在录音文字里找不到对应的原话，所以没有记成事项。如果确实漏了事，直接新建一条就行。",kind="system")
        activity.log(client.root,"error",f"会议「{meta.get('topic') or '会议'}」校验拦掉了 {len(dropped)} 处内容",detail="；".join(dropped),status="failed")
    try:  # 经历层: what was decided, asked, required and left waiting, with the words (and old waiting topics come back)
        from . import memory
        memory.record_meeting(client.root,review,meta)
        from . import hymem   # 记忆增强 (when switched on): the whole outcome of the meeting, to be extracted and reconciled
        parts=[(label,[getattr(e,"title",None) or e.text for e in entries]) for label,entries in
               (("定了",review.decisions),("要求",review.requirements),("还没定",review.open_questions),
                ("在等",getattr(review,"waiting",[]) or []),("要做",review.actions))]
        text="；".join(f"{label}：" + "、".join(x for x in items if x) for label,items in parts if items)
        if text:
            hymem.remember(client.root,"meeting",f"{str(meta.get('start_time') or '')[:10]} 会议《{meta.get('topic') or '会议'}》{text}",ref="meeting:"+review.meeting_id)
    except Exception as exc:
        activity.log(client.root,"error","会议经历没存进记忆",detail=f"{type(exc).__name__}: {exc}"[:200],status="failed")
    return {"ok":True,"meeting_id":review.meeting_id,"task_ids":task_ids,
            "dropped_low_confidence":len(review.actions)-len(kept_actions(review)),
            "message":line,"message_id":find_value(receipt,"message_id")}


def numbered_line(client, state, review, config, meta, task_ids):
    if not task_ids:
        return None
    from .records import read_records
    rows={r["record_id"]:r for r in read_records(client,state,"事项表",["编号","执行方","排除本人提醒"]) if r["record_id"] in task_ids}
    groups={"你":[],"AI":[],"别人":[]}
    for rid in task_ids:
        row=rows.get(rid) or {}
        if row.get("排除本人提醒"):
            continue
        executor=(row.get("执行方") or ["我"])[0]
        groups["你" if executor=="我" else executor].append(f"#{row.get('编号')}")
    total=sum(len(v) for v in groups.values())
    if not total:
        return None
    parts=" / ".join(k+" "+" ".join(v) for k,v in groups.items() if v)
    first=next(v[0] for v in groups.values() if v).lstrip("#")
    return f"{meta.get('topic') or '会议'} · 新增 {total} 件：{parts}\n回复：展开 {first} 看原话"


def backfill_meetings(root):
    """Once: earlier meetings' extractions become history too (quietly: no recall alerts for old news)."""
    import sqlite3
    db = memory._db(root)
    try:
        if db.execute("SELECT value FROM meta WHERE key='moments-backfill-v1'").fetchone():
            return 0
    finally:
        db.close()
    count = 0
    try:
        st = database.read(Path(root) / "state/state.db", timeout=10)
        queue = {mid: json.loads(meta) for mid, meta in st.execute("SELECT meeting_id, metadata FROM meeting_queue")}
        st.close()
    except sqlite3.Error:
        queue = {}
    for path in sorted((Path(root) / "state/meeting-transcripts").glob("*/review.json")):
        mid = path.parent.name
        try:
            review = Review.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        count += len(memory.record_meeting(root, review, {**queue.get(mid, {}), "meeting_id": mid}, announce=False))
    db = memory._db(root)
    try:
        with db:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('moments-backfill-v1', ?)", (memory._now(),))
    finally:
        db.close()
    return count


def main(argv=None):
    p=argparse.ArgumentParser(description="校验并发布有原话依据的会后复盘")
    p.add_argument("command",choices=["validate","publish"])
    p.add_argument("--review",type=Path,required=True)
    p.add_argument("--config",type=Path,default=Path("config.yaml"))
    args=p.parse_args(argv); path=args.config.resolve()
    try:
        config=load(path); review=Review.model_validate_json(args.review.read_text())
        with exclusive_lock(path.parent/"state",wait=90):
            state=State(path.parent/"state/state.db")
            try:
                row=MeetingQueue(state).get(review.meeting_id)
                if not row or not row["transcript_path"]:
                    raise MatrixError("未找到已准备的逐字稿。")
                transcript=(path.parent/row["transcript_path"]).read_text()
                meta={**row["metadata"],"owner_name":config.owner_name}
                # Salvage the good entries first; then the strict evidence check runs as before.
                review,repaired=repair_review(review,transcript,meta,config.owner_aliases)
                if repaired:
                    args.review.write_text(review.model_dump_json(indent=1))
                    key="meeting-repairs:"+review.meeting_id
                    state.set(key,list(dict.fromkeys([*(state.get(key) or []),*repaired])))
                validate_review(review,transcript,meta,config.owner_aliases)
                if args.command=="validate":
                    result={"ok":True,"actions":len(kept_actions(review)),"dropped_low_confidence":len(review.actions)-len(kept_actions(review)),
                            "remote_writes":0,"brief_line":summary_line(review,config,row["metadata"])}
                else:
                    client=Feishu(config,path.parent); client.verify_owner(client.auth())
                    result=publish(config,client,state,review)
                result["repaired"]=repaired
                print(js(result))
            finally:
                state.close()
        return 0
    except (MatrixError,OSError,ValueError) as exc:
        print(js({"ok":False,"error":str(exc) if isinstance(exc,MatrixError) else "复盘文件或结构校验失败，未发布。"}))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
