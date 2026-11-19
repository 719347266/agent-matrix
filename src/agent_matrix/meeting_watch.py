"""Durable, metadata-only meeting discovery. Transcript access is a separate gate."""
import argparse
import calendar
from datetime import datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

from .bitable import js, require_owned
from .config import load, save
from .feishu import Feishu
from .models import MatrixError, MatrixBusy
from .state import State, exclusive_lock

TZ = ZoneInfo("Asia/Shanghai")


def parse_time(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    except (ValueError, TypeError) as exc:
        raise MatrixError("会议时间格式异常，不猜测会议是否结束。") from exc


def month_ranges(start, end):
    """The search API accepts at most a month; do not drop older unhandled days."""
    while start <= end:
        last = start.replace(day=calendar.monthrange(start.year, start.month)[1], hour=23, minute=59, second=59)
        upper = min(last, end)
        yield start, upper
        start = upper + timedelta(seconds=1)


class MeetingQueue:
    def __init__(self, state):
        self.state = state
        self.db = state.db

    def get(self, meeting_id):
        row = self.db.execute("SELECT meeting_id,metadata,status,attempts,next_check,transcript_path,transcript_sha256,delivery_receipt FROM meeting_queue WHERE meeting_id=?", (meeting_id,)).fetchone()
        if not row:
            return None
        result = dict(zip(("meeting_id","metadata","status","attempts","next_check","transcript_path","transcript_sha256","delivery_receipt"), row))
        result["metadata"] = json.loads(result["metadata"])
        return result

    def list(self):
        return [self.get(row[0]) for row in self.db.execute("SELECT meeting_id FROM meeting_queue ORDER BY next_check,meeting_id")]

    def put(self, meta, now):
        mid = meta.get("meeting_id", "")
        if not re.fullmatch(r"\d{10,30}", mid):
            raise MatrixError("会议记录缺少有效 meeting_id。")
        if parse_time(meta.get("end_time")) > now:
            raise MatrixError("来源会议尚未结束，不进入会后复盘。")
        self.db.execute("INSERT OR IGNORE INTO meeting_queue(meeting_id,metadata,status,next_check) VALUES(?,?,'discovered',?)",
                        (mid, js(meta), now.isoformat()))
        self.db.commit()

    def update(self, mid, *, meta=None, status=None, attempts=None, next_check=None, transcript_path=None, transcript_sha256=None):
        values = {"metadata": js(meta) if meta else None, "status": status, "attempts": attempts,
                  "next_check": next_check, "transcript_path": transcript_path, "transcript_sha256": transcript_sha256}
        values = {k:v for k,v in values.items() if v is not None}
        if not values:
            return
        self.db.execute("UPDATE meeting_queue SET " + ",".join(k+"=?" for k in values)+" WHERE meeting_id=?", (*values.values(),mid))
        self.db.commit()

    def done(self, mid, receipt):
        # A meeting is acknowledged by a delivery receipt, or by tasks written plus
        # its line queued for the next brief (or no new tasks at all).
        from .bitable import find_value
        if not find_value(receipt, "message_id") and not (isinstance(receipt, dict) and "notice" in receipt):
            raise MatrixError("没有消息发送回执或简报记录，不标记会议处理完成。")
        self.db.execute("UPDATE meeting_queue SET status='done',delivery_receipt=? WHERE meeting_id=?", (js(receipt),mid))
        self.db.commit()


class MeetingSource:
    def __init__(self, client):
        self.client = client

    def agenda(self, now):
        start=now.replace(hour=0,minute=0,second=0,microsecond=0)
        data=self.client._exec(["calendar","+agenda","--start",start.isoformat(),"--end",
                               (start+timedelta(days=7)).isoformat(),"--as","user","--format","json"])
        if not isinstance(data,list):
            raise MatrixError("日程列表结构异常，本轮不把查询失败当作没有会议。")
        result=[]
        for event in data:
            if not isinstance(event,dict) or not event.get("event_id"):
                raise MatrixError("日程记录缺少唯一 ID，停止检查。")
            if event.get("self_rsvp_status")=="decline" or event.get("status")=="cancelled":
                continue
            result.append({key:event.get(key) for key in
                           ("event_id","summary","start_time","end_time","self_rsvp_status","app_link")})
        return result

    def search(self, start, end):
        found = {}
        for low, high in month_ranges(start,end):
            token, seen = "", set()
            while True:
                argv = ["vc", "+search", "--start", low.isoformat(timespec="seconds"), "--end", high.isoformat(timespec="seconds"),
                        "--participant-ids", self.client.config.owner_open_id, "--page-size", "30", "--as", "user", "--format", "json"]
                if token:
                    argv += ["--page-token", token]
                data = self.client._exec(argv)
                if not isinstance(data.get("items"),list) or not isinstance(data.get("has_more"),bool):
                    raise MatrixError("会议列表缺少完整分页信息，本轮停止。")
                for row in data["items"]:
                    if not isinstance(row.get("id"),str):
                        raise MatrixError("会议搜索结果缺少 ID。")
                    found[row["id"]] = row
                if not data["has_more"]:
                    break
                token = data.get("page_token")
                if not token or token in seen:
                    raise MatrixError("会议分页游标异常，本轮停止。")
                seen.add(token)
        return list(found.values())

    def detail(self, mid):
        data = self.client._exec(["vc", "+detail", "--meeting-ids", mid, "--as", "user", "--format", "json"])
        rows = data.get("meetings", [])
        if len(rows) != 1 or rows[0].get("meeting_id") != mid or rows[0].get("error"):
            raise MatrixError("未能取得对应会议详情，不当作没有逐字稿。")
        return rows[0]


def ingest(config, source, queue, mid, now):
    """Push path (listen.py): a meeting just ended or its notes were generated."""
    if not queue.get(mid):
        meta = source.detail(mid)
        if parse_time(meta.get("end_time")) > now:
            return "not_ended"
        queue.put(meta, now)
    row = queue.get(mid)
    if row["status"] in {"discovered", "waiting_transcript"}:
        meta = source.detail(mid)
        meta["source_url"] = row["metadata"].get("source_url", "")
        if meta.get("note_id") or meta.get("minute_token"):
            queue.update(mid, meta=meta, status="ready", next_check=now.isoformat())
        else:
            queue.update(mid, meta=meta, status="waiting_transcript", next_check=now.isoformat())
    return queue.get(mid)["status"]


def _search_due(queue, now):
    """Full re-scan every 30 minutes; in between the listener pushes new meetings."""
    last = queue.state.get("meeting_watch:last_search")
    beat = queue.state.get("listen:heartbeat")
    listening = beat and now - parse_time(beat) < timedelta(minutes=3)
    return not listening or not last or now - parse_time(last) >= timedelta(minutes=30)


def check(config, source, queue, now):
    if not config.meeting_watch_enabled or not config.meeting_watch_started_at:
        return {"ok":True, "run_model":False, "reason":"watch_disabled", "ready":[]}
    start = parse_time(config.meeting_watch_started_at)
    searching = _search_due(queue, now)
    scheduled = source.agenda(now) if searching else (queue.state.get("meeting_agenda_snapshot") or {}).get("events", [])
    # Re-read discovery range without advancing a cursor before delivery. Stable IDs
    # dedupe records. Pending meetings survive date changes and process restarts.
    for row in (source.search(start,now) if searching else []):
        if queue.get(row["id"]):
            continue
        meta = source.detail(row["id"])
        meta["source_url"] = row.get("meta_data",{}).get("app_link") or ""
        queue.put(meta,now)
    for row in queue.list():
        if row["status"] not in {"discovered","waiting_transcript"} or parse_time(row["next_check"]) > now:
            continue
        meta = source.detail(row["meeting_id"])
        meta["source_url"] = row["metadata"].get("source_url", "")
        if meta.get("note_id") or meta.get("minute_token"):
            queue.update(row["meeting_id"],meta=meta,status="ready")
        elif now-parse_time(meta["end_time"]) >= timedelta(hours=24):
            queue.update(row["meeting_id"],meta=meta,status="needs_attention")
        else:
            attempt = row["attempts"]+1
            delay = (1,2,5,10,30)[min(attempt-1,4)]
            queue.update(row["meeting_id"],meta=meta,status="waiting_transcript",attempts=attempt,
                         next_check=(now+timedelta(minutes=delay)).isoformat())
    ready = [{"meeting_id":r["meeting_id"],"topic":r["metadata"].get("topic"),"status":r["status"]}
             for r in queue.list() if r["status"] in {"ready","prepared","needs_attention"} and parse_time(r["next_check"])<=now]
    allowed = config.transcripts_to_llm_allowed and config.llm_backend=="cindy_current"
    actionable=any(r["status"]=="needs_attention" for r in ready) or (bool(ready) and allowed)
    # Calendar reservations remain metadata; elapsed scheduled time is not proof
    # a meeting actually happened and must never create a follow-up by itself.
    if searching:
        queue.state.set("meeting_agenda_snapshot",{"checked_at":now.isoformat(),"events":scheduled})
        queue.state.set("meeting_watch:last_search",now.isoformat())
    return {"ok":True,"run_model":actionable,"ready":ready,
            "scheduled_events_count":len(scheduled),
            "transcript_processing_allowed":allowed,"records":len(queue.list()),
            "reason":"actionable" if actionable else "awaiting_processing_permission" if ready else "idle"}


def defer(queue, mid, now):
    row=queue.get(mid)
    if not row or row["status"] in {"done","attention_sent"}:
        raise MatrixError("会议不存在或已处理，不改变状态。")
    old=row["attempts"]+1
    exhausted=now-parse_time(row["metadata"]["end_time"])>=timedelta(hours=24)
    queue.update(mid,status="needs_attention" if exhausted else "waiting_transcript",attempts=old,
                 next_check=(now+timedelta(minutes=5 if old<3 else 30)).isoformat())
    return {"ok":True,"meeting_id":mid,"status":"needs_attention" if exhausted else "waiting_transcript"}


def notify_missing(config,client,queue,mid):
    row=queue.get(mid)
    if not row or row["status"] not in {"needs_attention","attention_sent"}:
        raise MatrixError("会议不在需要人工补充的队列中。")
    key="meeting-missing-"+sha256(mid.encode()).hexdigest()[:24]
    receipt=queue.state.once(key,lambda:client.send_self(
        f"【会后待补充】{row['metadata'].get('topic','会议')}\n已结束超过24小时，仍未取得可用逐字稿，暂时无法自动复盘。你可以直接发会议要点，我再整理结论和待办。",key))
    from .bitable import find_value
    if not find_value(receipt,"message_id"):
        raise MatrixError("提醒缺少发送回执，不标记已通知。")
    queue.update(mid,status="attention_sent")
    return {"ok":True,"meeting_id":mid,"message_id":find_value(receipt,"message_id")}


def prepare(config, client, queue, mid):
    if not config.transcripts_to_llm_allowed or config.llm_backend != "cindy_current":
        raise MatrixError("逐字稿交给模型的权限尚未确认；只检测会议，不读取正文。")
    row = queue.get(mid)
    if not row or row["status"] not in {"ready","prepared"}:
        raise MatrixError("会议不在已就绪队列中，不能处理。")
    if row["transcript_path"]:
        path = client.root/row["transcript_path"]
        if path.is_file() and sha256(path.read_bytes()).hexdigest()==row["transcript_sha256"]:
            queue.update(mid,next_check=(datetime.now(TZ)+timedelta(minutes=30)).isoformat())
            return {"meeting_id":mid,"transcript_path":str(path),"transcript_sha256":row["transcript_sha256"],"metadata":row["metadata"]}
        raise MatrixError("缓存逐字稿与记录不一致，不覆盖已有文件。")
    directory = client.root/"state/meeting-transcripts"/mid
    directory.mkdir(parents=True,exist_ok=True)
    os.chmod(directory,0o700)
    path = directory/"transcript.md"
    meta = row["metadata"]
    if meta.get("note_id"):
        data = client._exec(["note","+detail","--note-id",meta["note_id"],"--as","user","--format","json"])
        note = data.get("note",{})
        display = note.get("note_display_type")
        if display=="unified":
            client._exec(["note","+transcript","--note-id",meta["note_id"],"--as","user","--output",path.relative_to(client.root).as_posix()])
        elif display in {"normal","unknown"} and note.get("verbatim_doc_token"):
            data = client._exec(["docs","+fetch","--doc",note["verbatim_doc_token"],"--doc-format","markdown","--as","user"])
            # Explicit content fields only, never fall back to AI note text.
            text = data.get("document",{}).get("content")
            if not isinstance(text,str) or not text.strip():
                raise MatrixError("逐字稿返回格式与已支持格式不符，需要核对；不会改用 AI 纪要。")
            with path.open("x") as stream:
                stream.write(text)
            response=directory/"transcript-response.json"
            response.write_text(js(data))
            os.chmod(response,0o600)
        else:
            raise MatrixError("逐字稿入口尚不明确，需稍后补查。")
    else:
        # Minutes output is file-based; only use the actual returned artifact path.
        data = client._exec(["minutes","+detail","--minute-tokens",meta["minute_token"],"--transcript",
                             "--output-dir",directory.relative_to(client.root).as_posix(),"--as","user"])
        entries = data.get("minutes",[])
        if len(entries)!=1 or entries[0].get("minute_token")!=meta["minute_token"]:
            raise MatrixError("妙记逐字稿返回不完整，停止处理。")
        source_path = Path(entries[0].get("artifacts",{}).get("transcript_file") or "")
        source_path = source_path if source_path.is_absolute() else client.root/source_path
        if not source_path.resolve().is_relative_to(directory.resolve()) or not source_path.is_file():
            raise MatrixError("逐字稿文件路径不在本轮目录内，停止处理。")
        with path.open("x") as stream:
            stream.write(source_path.read_text())
    if not path.is_file() or not path.read_text().strip():
        raise MatrixError("逐字稿尚为空，未执行提取。")
    os.chmod(path,0o600)
    digest = sha256(path.read_bytes()).hexdigest()
    queue.update(mid,status="prepared",transcript_path=str(path.relative_to(client.root)),transcript_sha256=digest,
                 next_check=(datetime.now(TZ)+timedelta(minutes=30)).isoformat())
    # The things already in flight, so the model can say "this is #12 again" (same_as) instead of a new item.
    open_path=path.parent/"open_items.json"
    try:
        from .workflow import Gateway
        from .merge import open_items
        rows=Gateway(client,queue.state).rows()
        items=[{"编号":int(str(r["编号"])),"标题":r.get("标题"),"谁做":(r.get("执行方") or [""])[0],"状态":(r.get("状态") or [""])[0]}
               for r in open_items(rows) if not r.get("排除本人提醒")]
        open_path.write_text(json.dumps(items,ensure_ascii=False,indent=1))
    except Exception:
        open_path=None
    return {"meeting_id":mid,"transcript_path":str(path),"transcript_sha256":digest,"metadata":meta,
            "open_items_path":str(open_path) if open_path else None}


def main(argv=None):
    parser=argparse.ArgumentParser(description="会议结束检测与待处理队列")
    parser.add_argument("command",choices=["init","check","status","prepare","defer","notify-missing"])
    parser.add_argument("--config",type=Path,default=Path("config.yaml"))
    parser.add_argument("--meeting-id")
    args=parser.parse_args(argv)
    path=args.config.resolve()
    try:
        config=load(path)
        now=datetime.now(TZ)
        if args.command=="init":
            config.meeting_watch_enabled=True
            if not config.meeting_watch_started_at:
                config.meeting_watch_started_at=now.replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
            save(path,config)
            print(js({"ok":True,"started_at":config.meeting_watch_started_at,"processing_allowed":config.transcripts_to_llm_allowed}))
            return 0
        with exclusive_lock(path.parent/"state",wait=0 if args.command=="check" else 90):
            state=State(path.parent/"state/state.db")
            try:
                queue=MeetingQueue(state)
                if args.command=="status":
                    result={"ok":True,"watch_enabled":config.meeting_watch_enabled,"processing_allowed":config.transcripts_to_llm_allowed,
                            "agenda":state.get("meeting_agenda_snapshot",{}),
                            "meetings":[{"meeting_id":r["meeting_id"],"topic":r["metadata"].get("topic"),"status":r["status"],"attempts":r["attempts"]} for r in queue.list()]}
                elif args.command=="defer":
                    result=defer(queue,args.meeting_id,now)
                else:
                    client=Feishu(config,path.parent)
                    client.verify_owner(client.auth())
                    require_owned(config,state)
                    if args.command=="check":
                        result=check(config,MeetingSource(client),queue,now)
                    elif args.command=="notify-missing":
                        result=notify_missing(config,client,queue,args.meeting_id)
                    else:
                        result=prepare(config,client,queue,args.meeting_id)
                print(js(result))
                if args.command=="check":
                    from . import activity
                    activity.beat(path.parent,"会议检查",f"待处理 {len(result.get('ready',[]))} 场")
                return 2 if args.command=="check" and not result["run_model"] else 0
            finally:
                state.close()
    except MatrixBusy:
        print(js({"ok":True,"run_model":False,"reason":"busy"}))
        return 2
    except (MatrixError,OSError,ValueError) as exc:
        from . import activity
        activity.log(path.parent,"error",f"会议 {args.command} 出错",detail=str(exc),status="failed")
        print(js({"ok":False,"error":str(exc) if isinstance(exc,MatrixError) else "本地会议队列异常，本轮停止。"}),file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
