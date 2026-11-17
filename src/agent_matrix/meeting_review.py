"""Validate a model's grounded review, then publish to the owner's existing Base."""
import argparse
from datetime import date, timedelta
from hashlib import sha256, sha1
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
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


MIN_CONFIDENCE=0.8


class Review(BaseModel):
    model_config=ConfigDict(extra="forbid")
    meeting_id: str
    transcript_sha256: str
    decisions: list[Grounded] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    requirements: list[Grounded] = Field(default_factory=list)
    open_questions: list[Grounded] = Field(default_factory=list)
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
    for entry in [*review.decisions,*review.actions,*review.requirements,*review.open_questions]:
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
    return review


def task_fields(action, config, meta):
    # Other names remain in the evidence until a unique participant ID is resolved.
    own=bool(action.owner_name and config.owner_name.strip() and config.owner_open_id.strip()
             and action.owner_name==config.owner_name)
    if own:
        executor="AI" if action.executor=="AI" else "我"
    else:
        executor="别人" if action.direction in {"others","others_to_me"} or action.owner_name else (action.executor or "我")
    hidden=executor=="别人" and action.direction!="others_to_me"
    ready=bool(action.due_date) and (own or executor=="别人")
    status="未开始" if ready else "待确认"
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
    for table,required in (("事项表",["标题","状态","原话引用","去重键"]),("会议处理记录表",["会议ID","提取状态","提取待办数"])):
        fields=items(client.call("+field-list",*coords,"--table-id",config.tables[table]))
        names={f.get("name",f.get("field_name")) for f in fields}
        if not set(required)<=names:
            raise MatrixError("复盘需要的字段已变化，停止写入："+table)
    task_ids=[]
    for action in kept_actions(review):
        fields=task_fields(action,config,row["metadata"])
        receipt=state.once(prefix+":"+fields["去重键"],lambda f=fields:client.call("+record-batch-create",*coords,
                           "--table-id",config.tables["事项表"],"--json",js({"create_records":[f]}),write=True))
        ids=receipt.get("record_id_list")
        if not isinstance(ids,list) or len(ids)!=1:
            raise MatrixError("事项创建回执缺少唯一记录 ID，停止。")
        task_ids.extend(ids)
    meta=row["metadata"]
    record={"会议ID":review.meeting_id,"标题":meta.get("topic"),"开始时间":parse_time(meta["start_time"]).isoformat(),
            "结束时间":parse_time(meta["end_time"]).isoformat(),"逐字稿状态":["已获取"],"提取状态":["已完成"],"提取待办数":len(task_ids)}
    state.once(prefix+":meeting-record",lambda:client.call("+record-batch-create",*coords,"--table-id",config.tables["会议处理记录表"],
                                                         "--json",js({"create_records":[record]}),write=True))
    # Owner wants control: right after the meeting, one line with the new numbers.
    # A meeting with no new tasks sends nothing.
    try:
        line=numbered_line(client,state,review,config,meta,task_ids)
    except (MatrixError,AttributeError):
        line=summary_line(review,config,meta)  # numbers unreadable right now: counts only
    if line:
        key="review-"+sha256(review.meeting_id.encode()).hexdigest()[:32]
        receipt=state.once(prefix+":notify",lambda:client.send_self(line,key))
    else:
        receipt={"notice":None,"task_ids":task_ids}
    queue.done(review.meeting_id,receipt)
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
                validate_review(review,(path.parent/row["transcript_path"]).read_text(),
                                {**row["metadata"],"owner_name":config.owner_name},config.owner_aliases)
                if args.command=="validate":
                    result={"ok":True,"actions":len(kept_actions(review)),"dropped_low_confidence":len(review.actions)-len(kept_actions(review)),
                            "remote_writes":0,"brief_line":summary_line(review,config,row["metadata"])}
                else:
                    client=Feishu(config,path.parent); client.verify_owner(client.auth())
                    result=publish(config,client,state,review)
                print(js(result))
            finally:
                state.close()
        return 0
    except (MatrixError,OSError,ValueError) as exc:
        print(js({"ok":False,"error":str(exc) if isinstance(exc,MatrixError) else "复盘文件或结构校验失败，未发布。"}))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
