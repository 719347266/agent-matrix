"""End-to-end scenarios on the isolated copy. Start them with tools/e2e/run.sh, not directly.

Pushed events go straight into the listener's own entry point (Listener.dispatch); the AI side is played
through the driver protocol (drive.next_job / drive.submit), writing the files an AI would write.
Every check reads the result back from the databases or the local inbox.
"""
from datetime import datetime, timedelta
from hashlib import sha256
import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path.cwd()
import pwd  # noqa: E402
assert (ROOT / "world.json").exists() and Path.home() != Path(pwd.getpwuid(os.getuid()).pw_dir), \
    "run through tools/e2e/run.sh: inside the copy, with a test HOME"

from agent_matrix import db, drive, inbox  # noqa: E402
from agent_matrix.config import load  # noqa: E402
from agent_matrix.listen import Listener  # noqa: E402
from agent_matrix.timeutil import TZ  # noqa: E402

CONFIG = load(ROOT / "config.yaml")
OWNER, OWNER_NAME = CONFIG.owner_open_id, CONFIG.owner_name
LISTENER = Listener(ROOT / "config.yaml")
NOW = datetime.now(TZ)
results = []


def check(scenario, what, ok, evidence=""):
    results.append({"scenario": scenario, "check": what, "ok": bool(ok), "evidence": str(evidence)[:300]})
    print(("  ✓ " if ok else "  ✗ ") + what + (f"  —  {evidence}" if evidence and not ok else ""), flush=True)


def world(update=None):
    data = json.loads((ROOT / "world.json").read_text())
    if update:
        update(data)
        (ROOT / "world.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return data


def items():
    conn = db.read(ROOT / "state/items.db")
    try:
        out = {}
        for rid, n, fields in conn.execute("SELECT record_id, n, fields FROM items"):
            f = json.loads(fields)
            f["record_id"], f["n"] = rid, n
            out[rid] = f
        return out
    finally:
        conn.close()


def one(value):
    return value[0] if isinstance(value, list) and len(value) == 1 else value


def by_key(prefix):
    return [r for r in items().values() if str(r.get("去重键") or "").startswith(prefix)]


def inbox_since(marker):
    conn = db.read(ROOT / "state/inbox.db")
    try:
        return [{"id": i, "key": k, "kind": kind, "title": t, "summary": (s or "") + " " + (b or "")} for i, k, kind, t, s, b in
                conn.execute("SELECT id, key, kind, title, summary, blocks FROM messages WHERE id>? ORDER BY id", (marker,))]
    finally:
        conn.close()


def inbox_marker():
    conn = db.read(ROOT / "state/inbox.db")
    try:
        return conn.execute("SELECT coalesce(max(id),0) FROM messages").fetchone()[0]
    finally:
        conn.close()


def state_get(key):
    conn = db.read(ROOT / "state/state.db")
    try:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def meeting_row(mid):
    conn = db.read(ROOT / "state/state.db")
    try:
        row = conn.execute("SELECT status, attempts, delivery_receipt FROM meeting_queue WHERE meeting_id=?", (mid,)).fetchone()
        return row
    finally:
        conn.close()


def dispatch(key, event):
    """What the listener does when Feishu pushes this event; returns the handler's result."""
    captured = {}
    original = LISTENER.locked

    def locked(job):
        captured["result"] = original(job)
        return captured["result"]

    LISTENER.locked = locked
    try:
        LISTENER.dispatch(key, event)
    finally:
        LISTENER.locked = original
    return captured.get("result", "<failed: see activity log>")


def fake_calls():
    path = ROOT / "fake-lark-calls.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def owner_says(text, mid):
    return dispatch("im.message.receive_v1", {"chat_type": "p2p", "sender_id": {"open_id": OWNER}, "message_id": mid,
                                              "chat_id": "oc_sandbox", "content": json.dumps({"text": text}, ensure_ascii=False)})


# ---------------------------------------------------------------------------------------------------------
def scenario_meeting():
    print("\n[1] 会议：结束推送 → 逐字稿推送 → AI 提取 → 入表 → 通知本人", flush=True)
    S = "会议"
    mid, note = "9000000000000000001", "sbx-note-1"
    start = (NOW - timedelta(minutes=50)).replace(microsecond=0)
    meta = {"meeting_id": mid, "topic": "【测试】渠道复盘会", "start_time": start.isoformat(),
            "end_time": (NOW + timedelta(minutes=10)).replace(microsecond=0).isoformat()}
    transcript = "\n".join([
        f'[00:01:05] <cite user-name="{OWNER_NAME}"></cite>：渠道复盘报告明天我来出一版，重点看投放回收。',
        '[00:02:10] <cite user-name="王小明"></cite>：投放素材我负责，下周给到大家。',
        f'[00:03:30] <cite user-name="{OWNER_NAME}"></cite>：定了，这个季度先停掉抖音信息流。',
        f'[00:04:00] <cite user-name="{OWNER_NAME}"></cite>：供应商那边我来约一次会，把报价谈清楚。',
        '[00:05:20] <cite user-name="王小明"></cite>：我们后面可以看看小红书。',
    ]) + "\n"
    world(lambda w: w["meetings"].__setitem__(mid, meta))

    r = dispatch("vc.meeting.participant_meeting_ended_v1", {"meeting_id": mid})
    check(S, "会议还没到结束时间：不排队", r == "not_ended" and meeting_row(mid) is None, r)

    world(lambda w: w["meetings"][mid].__setitem__("end_time", (NOW - timedelta(minutes=2)).replace(microsecond=0).isoformat()))
    r = dispatch("vc.meeting.participant_meeting_ended_v1", {"meeting_id": mid})
    check(S, "会议结束推送：进队列，等逐字稿", r == "waiting_transcript", r)

    world(lambda w: (w["meetings"][mid].__setitem__("note_id", note), w["transcripts"].__setitem__(note, transcript)))
    r = dispatch("vc.note.generated_v1", {"note_source": {"source_entity_id": mid}})
    check(S, "逐字稿生成推送：标记就绪", r == "ready", r)
    r = dispatch("vc.note.generated_v1", {"note_source": {"source_entity_id": mid}})
    check(S, "同一推送重复到达：状态不变、不重复排队", r == "ready" and meeting_row(mid)[0] == "ready", r)

    before = inbox_marker()
    earlier = next(i for i in items().values() if one(i.get("状态")) not in ("取消",) and not i.get("排除本人提醒"))
    job = drive.next_job(ROOT, CONFIG, "测试AI", kinds=["meeting"])
    check(S, "AI 领到「提取会议待办」这件活", job and job["kind"] == "meeting" and job["ref"]["meeting_id"] == mid,
          job and {k: job.get(k) for k in ("id", "kind", "title")})
    if not job:
        return {}
    folder = Path(job["folder"])
    got = (folder / "transcript.md").read_text()
    check(S, "活的文件夹里有逐字稿和未完成事项清单", got == transcript and (folder / "open_items.json").exists())
    related = json.loads((folder / "related_items.json").read_text()) if (folder / "related_items.json").exists() else []
    check(S, "还有一份「可能接着哪件事做」的清单", any(x["编号"] == earlier["n"] for x in related), len(related))
    digest = sha256(got.encode()).hexdigest()
    lines = transcript.splitlines()
    due = (start.date() + timedelta(days=1)).isoformat()
    review = {
        "meeting_id": mid, "transcript_sha256": digest,
        "decisions": [{"text": "本季度停掉抖音信息流", "quote": "这个季度先停掉抖音信息流", "timestamp": "00:03:30", "reference": lines[2]}],
        "actions": [
            {"title": "【测试】渠道复盘报告", "text": "出一版渠道复盘报告，重点看投放回收", "quote": "渠道复盘报告明天我来出一版",
             "timestamp": "00:01:05", "reference": lines[0], "owner_name": OWNER_NAME, "due_date": due, "due_phrase": "明天",
             "direction": "mine", "commitment_phrase": "我来", "confidence": 0.95, "executor": "AI",
             "done_when": "有回收对比和结论", "follows": earlier["n"]},
            {"title": "【测试】约供应商谈报价", "text": "约供应商开会谈报价", "quote": "供应商那边我来约一次会",
             "timestamp": "00:04:00", "reference": lines[3], "owner_name": OWNER_NAME, "direction": "mine",
             "commitment_phrase": "我来", "confidence": 0.9, "executor": "我", "follows": 99999},
            {"title": "【测试】投放素材", "text": "准备投放素材", "quote": "投放素材我负责", "timestamp": "00:02:10",
             "reference": lines[1], "owner_name": "王小明", "direction": "others", "commitment_phrase": "我负责",
             "confidence": 0.9},
        ],
    }
    (folder / "review.json").write_text(json.dumps(review, ensure_ascii=False))
    out = drive.submit(ROOT, CONFIG, job["id"])
    check(S, "AI 交活：程序校验原话后发布", out.get("ok") and out.get("status") == "done", out)

    made = {r["标题"]: r for r in by_key("meeting:" + mid)}
    check(S, "3 条待办入表（「后面可以看看」没算待办）", len(made) == 3, sorted(made))
    ai = made.get("【测试】渠道复盘报告", {})
    me = made.get("【测试】约供应商谈报价", {})
    other = made.get("【测试】投放素材", {})
    check(S, "本人的 AI 活：执行方 AI、未开始、截止日按「明天」换算",
          one(ai.get("执行方")) == "AI" and one(ai.get("状态")) == "未开始" and str(ai.get("截止日", "")).startswith(due),
          {k: ai.get(k) for k in ("执行方", "状态", "截止日")})
    from agent_matrix import itemlinks
    check(S, "AI 说这件事接着另一件做：两件事被串起来，标明是 AI 串的",
          itemlinks.made_by(ROOT).get((earlier["record_id"], ai.get("record_id"))) == "AI", itemlinks.pairs(ROOT))
    check(S, "AI 填了不存在的编号：不串、也不影响建事项", itemlinks.direct(ROOT, me.get("record_id")) == {"before": [], "after": []})
    check(S, "本人自己做的、没说日期：待确认", one(me.get("执行方")) == "我" and one(me.get("状态")) == "待确认",
          {k: me.get(k) for k in ("执行方", "状态")})
    check(S, "别人负责的：执行方别人、不提醒本人", one(other.get("执行方")) == "别人" and other.get("排除本人提醒") is True,
          {k: other.get(k) for k in ("执行方", "排除本人提醒")})
    sent = inbox_since(before)
    line = next((m for m in sent if m["key"].startswith("review-")), None)
    check(S, "本人收到一条会后通知，只列自己和 AI 的编号",
          line and f"#{ai.get('n')}" in line["title"] + line["summary"] and f"#{other.get('n')}" not in line["title"] + line["summary"],
          [m["title"] for m in sent])
    check(S, "会议标记处理完成", meeting_row(mid)[0] == "done", meeting_row(mid))
    conn = db.read(ROOT / "state/xinxiang.db")
    try:
        moments = conn.execute("SELECT kind, text FROM moments WHERE source_id=?", (mid,)).fetchall()
    finally:
        conn.close()
    check(S, "「定了」的决定进了记忆", ("decision", "本季度停掉抖音信息流") in moments, moments)
    writes = [w for w in world().get("base_writes", []) if w["table"] == CONFIG.tables.get("会议处理记录表")]
    check(S, "会议处理记录写了一条", len(writes) == 1, len(writes))

    # Replays: the same push again, the same submit again, another AI asking for work
    r = dispatch("vc.meeting.participant_meeting_ended_v1", {"meeting_id": mid})
    again = drive.submit(ROOT, CONFIG, job["id"])
    nxt = drive.next_job(ROOT, CONFIG, "另一个AI", kinds=["meeting"])
    check(S, "重放推送 / 重复交活 / 别的 AI 再领：都不重复建项或发消息",
          r == "done" and again.get("note") and nxt is None and len(by_key("meeting:" + mid)) == 3
          and len([m for m in inbox_since(before) if m["key"].startswith("review-")]) == 1,
          {"push": r, "resubmit": again, "next": nxt and nxt["id"]})
    return {"ai": ai, "me": me, "other": other}


def scenario_quiet_meeting():
    print("\n[2] 会议没有新待办：不打扰本人", flush=True)
    S = "无待办会议"
    mid, note = "9000000000000000002", "sbx-note-2"
    start = (NOW - timedelta(minutes=40)).replace(microsecond=0)
    transcript = f'[00:00:30] <cite user-name="{OWNER_NAME}"></cite>：今天就同步一下进度，结论是方向不变。\n'
    world(lambda w: (w["meetings"].__setitem__(mid, {"meeting_id": mid, "topic": "【测试】周会同步", "start_time": start.isoformat(),
                                                     "end_time": (NOW - timedelta(minutes=1)).replace(microsecond=0).isoformat(),
                                                     "note_id": note}),
                     w["transcripts"].__setitem__(note, transcript)))
    r = dispatch("vc.note.generated_v1", {"note_source": {"source_entity_id": mid}})
    check(S, "逐字稿推送直接就绪", r == "ready", r)
    before = inbox_marker()
    job = drive.next_job(ROOT, CONFIG, "测试AI", kinds=["meeting"])
    if not job:
        check(S, "AI 领到提取活", False)
        return
    folder = Path(job["folder"])
    review = {"meeting_id": mid, "transcript_sha256": sha256((folder / "transcript.md").read_bytes()).hexdigest(),
              "decisions": [{"text": "方向不变", "quote": "结论是方向不变", "timestamp": "00:00:30", "reference": transcript.strip()}]}
    (folder / "review.json").write_text(json.dumps(review, ensure_ascii=False))
    out = drive.submit(ROOT, CONFIG, job["id"])
    check(S, "交活成功、0 件新事项", out.get("ok") and out.get("created") == 0 and not by_key("meeting:" + mid), out)
    check(S, "本人收件箱没有新消息", inbox_since(before) == [], [m["title"] for m in inbox_since(before)])


def scenario_bad_extraction():
    print("\n[3] AI 编造原话：被程序拦下，不入表", flush=True)
    S = "防编造"
    mid, note = "9000000000000000003", "sbx-note-3"
    start = (NOW - timedelta(minutes=30)).replace(microsecond=0)
    transcript = f'[00:00:10] <cite user-name="{OWNER_NAME}"></cite>：大家先看看，下次再说。\n'
    world(lambda w: (w["meetings"].__setitem__(mid, {"meeting_id": mid, "topic": "【测试】闲聊会", "start_time": start.isoformat(),
                                                     "end_time": (NOW - timedelta(minutes=1)).replace(microsecond=0).isoformat(),
                                                     "note_id": note}),
                     w["transcripts"].__setitem__(note, transcript)))
    dispatch("vc.note.generated_v1", {"note_source": {"source_entity_id": mid}})
    job = drive.next_job(ROOT, CONFIG, "测试AI", kinds=["meeting"])
    if not job:
        check(S, "AI 领到提取活", False)
        return
    folder = Path(job["folder"])
    fake = {"meeting_id": mid, "transcript_sha256": sha256((folder / "transcript.md").read_bytes()).hexdigest(),
            "actions": [{"title": "【测试】编造的待办", "text": "x", "quote": "我来做竞品分析", "timestamp": "00:00:10",
                         "reference": "[00:00:10] 我来做竞品分析", "owner_name": OWNER_NAME, "direction": "mine",
                         "commitment_phrase": "我来", "confidence": 0.99}]}
    (folder / "review.json").write_text(json.dumps(fake, ensure_ascii=False))
    before = inbox_marker()
    out = drive.submit(ROOT, CONFIG, job["id"])
    check(S, "原话对不上的那条被自动去掉，没有入表", out.get("ok") and not by_key("meeting:" + mid), out)
    warned = [m for m in inbox_since(before) if "和录音对不上，没有采用" in m["title"] + m["summary"]]
    check(S, "本人收到「有几条内容和录音对不上，没有采用」的提示", bool(warned), [m["title"] for m in inbox_since(before)])


def write_delivery(folder, version_note):
    body = (f"# 渠道复盘报告\n\n## 推荐\n建议本季度把预算从抖音信息流转向搜索和私域，回收周期更短。{version_note}\n\n"
            "## 对比\n| 渠道 | 7日回收 |\n|---|---|\n| 抖音信息流 | 38% |\n| 搜索 | 61% |\n\n"
            "搜索渠道的七日回收明显更高，私域的复购也更稳定，抖音信息流的获客成本连续三周上升。\n\n"
            "## 拍板\n是否同意下周起把抖音信息流预算减半，先观察两周回收数据再定。\n\n"
            "## 用到的资料\n- 会议逐字稿\n\n## 我做的假设\n- 回收数据按最近四周口径\n")
    (folder / "交付.md").write_text(body, encoding="utf-8")
    (folder / "结果.json").write_text(json.dumps({"outcome": "done", "template": "方案", "summary": "建议抖音预算减半转搜索",
                                                  "confirmation": "是否同意预算减半", "sources": ["会议逐字稿"]},
                                                 ensure_ascii=False), encoding="utf-8")


def take_item(record_id, driver="测试AI"):
    others = [rid for rid in items() if rid != record_id]
    job = drive.next_job(ROOT, CONFIG, driver, kinds=["item"], skip=others)
    return job if job and job["ref"]["record_id"] == record_id else (job and drive.release(ROOT, CONFIG, job["id"]) and None)


def scenario_ai_work(made):
    print("\n[4] AI 做事：领活 → 交付 → 待验收 → 本人退回 → 改完再交 → 本人通过", flush=True)
    S = "AI 做事与验收"
    ai = made.get("ai")
    if not ai:
        check(S, "前置：会议建出的 AI 活", False)
        return
    rid, n = ai["record_id"], ai["n"]
    before = inbox_marker()
    job = take_item(rid)
    check(S, f"AI 领到 #{n}", bool(job), job and job["id"])
    if not job:
        return
    folder = Path(job["folder"])
    check(S, "任务单里有本人要求和会议原话", "渠道复盘报告" in (job.get("instructions") or "") + "".join(
        p.read_text(errors="ignore") for p in folder.glob("*.md")))
    (folder / "交付.md").write_text("太短", encoding="utf-8")
    (folder / "结果.json").write_text(json.dumps({"outcome": "done", "template": "方案", "summary": "x", "sources": ["a"]}), encoding="utf-8")
    bad = drive.submit(ROOT, CONFIG, job["id"])
    check(S, "交付不合格（缺小节、太短）：程序退回让 AI 改", bad.get("status") == "fix", bad)
    write_delivery(folder, "")
    out = drive.submit(ROOT, CONFIG, job["id"])
    row = items()[rid]
    check(S, "改好再交：发布成功，事项变「待验收」并带交付链接",
          out.get("status") == "done" and one(row.get("状态")) == "待验收" and "docx" in str(row.get("交付物链接")), out)
    sent = [m for m in inbox_since(before)]
    check(S, "本人收件箱收到「请你验收」", any(f"#{n}" in m["title"] + m["summary"] for m in sent), [m["title"] for m in sent])

    before = inbox_marker()
    r = owner_says(f"{n} 退 补上私域渠道的数据", "om_sbx_reject")
    row = items()[rid]
    check(S, "本人私聊「退」：事项回到进行中、记下退回意见",
          r == "handled" and one(row.get("状态")) == "进行中" and "私域" in str(row.get("退回意见")), {"result": r, "状态": row.get("状态")})
    check(S, "本人收到确认回执", any("✓" in m["title"] + m["summary"] for m in inbox_since(before)), [m["title"] for m in inbox_since(before)])
    r = owner_says(f"{n} 退 补上私域渠道的数据", "om_sbx_reject")
    check(S, "同一条私聊重复推送：不再处理", r == "seen", r)

    job2 = take_item(rid)
    check(S, "AI 重新领到同一件事（带着退回意见）", bool(job2) and job2["id"] != job["id"], job2 and job2["id"])
    if not job2:
        return
    text = (job2.get("instructions") or "") + "".join(p.read_text(errors="ignore") for p in Path(job2["folder"]).glob("*.md"))
    check(S, "第二轮任务单里能看到「补上私域」", "私域" in text)
    stale = drive.submit(ROOT, CONFIG, job["id"])
    check(S, "旧那轮的 AI 不能再交", stale.get("note") or stale.get("status") in {"done", "cancelled"}, stale)
    write_delivery(Path(job2["folder"]), "私域复购率 42%，高于其他渠道。")
    out = drive.submit(ROOT, CONFIG, job2["id"])
    row = items()[rid]
    conn = db.read(ROOT / "state/xinxiang.db")
    try:
        versions = [v for (v,) in conn.execute("SELECT version FROM deliverables WHERE record_id=? ORDER BY version", (rid,))]
    finally:
        conn.close()
    check(S, "第二版交付：再次待验收，交付记录有 v1、v2", out.get("status") == "done" and one(row.get("状态")) == "待验收"
          and versions[-2:] == [1, 2], {"out": out, "versions": versions})

    r = owner_says(f"{n} 过", "om_sbx_pass")
    row = items()[rid]
    check(S, "本人私聊「过」：完成、验收结果通过", one(row.get("状态")) == "完成" and one(row.get("验收结果")) == "通过",
          {k: row.get(k) for k in ("状态", "验收结果")})
    r = owner_says(f"{n} 过", "om_sbx_pass_again")
    row2 = items()[rid]
    check(S, "已完成的事再说「过」：不改动", one(row2.get("状态")) == "完成" and row2.get("更新时间") == row.get("更新时间"), r)

    r = dispatch("im.message.receive_v1", {"chat_type": "p2p", "sender_id": {"open_id": "ou_someone_else"}, "message_id": "om_sbx_stranger",
                                           "content": json.dumps({"text": f"{n} 不做"})})
    check(S, "别人发来的指令：忽略", r == "ignored" and one(items()[rid].get("状态")) == "完成", r)


def scenario_card(made):
    print("\n[5] 卡片按钮推送", flush=True)
    S = "卡片按钮"
    me = made.get("me")
    if not me:
        check(S, "前置：会议建出的本人事项", False)
        return
    rid, n = me["record_id"], me["n"]
    event = {"operator_id": OWNER, "action_tag": "button", "event_id": "ev_sbx_card_1", "message_id": "om_sbx_card",
             "action_value": json.dumps({"cmd": f"{n} 完成"}, ensure_ascii=False)}
    r = dispatch("card.action.trigger", event)
    check(S, f"点「完成」：#{n} 变完成", r in {"handled", "updated"} and one(items()[rid].get("状态")) == "完成", r)
    r = dispatch("card.action.trigger", event)
    check(S, "同一次点击重复推送：不再处理", r == "seen", r)
    r = dispatch("card.action.trigger", {**event, "event_id": "ev_sbx_card_2", "operator_id": "ou_someone_else"})
    check(S, "别人点按钮：忽略", r == "ignored", r)


def scenario_feishu_task():
    print("\n[6] 飞书任务推送", flush=True)
    S = "飞书任务"
    guid = "sbx-task-guid-1"
    task = {"guid": guid, "summary": "【测试】整理十二月活动排期", "url": f"https://applink.feishu.cn/client/todo/detail?guid={guid}",
            "members": [{"id": OWNER, "role": "assignee"}], "created_at": str(int(NOW.timestamp() * 1000))}
    world(lambda w: w.__setitem__("tasks", [task]))
    before = inbox_marker()
    r = dispatch("task.task.update_user_access_v2", {})
    made = by_key("feishu-task:" + guid)
    check(S, "分给本人的新任务：建成事项", isinstance(r, dict) and r.get("created") == 1 and len(made) == 1, r)
    check(S, "本人收到「飞书任务 · 新增」", any("飞书任务" in m["title"] for m in inbox_since(before)), [m["title"] for m in inbox_since(before)])
    r = dispatch("task.task.update_user_access_v2", {})
    check(S, "再推一次：不重复建", r.get("created") == 0 and len(by_key("feishu-task:" + guid)) == 1, r)
    world(lambda w: w["tasks"][0].__setitem__("completed_at", NOW.isoformat()))
    r = dispatch("task.task.update_user_access_v2", {})
    check(S, "本人在飞书里勾完成：事项跟着完成", r.get("completed") == 1 and one(by_key("feishu-task:" + guid)[0].get("状态")) == "完成", r)
    world(lambda w: w["tasks"].append({**task, "guid": "sbx-task-guid-2", "summary": "【测试】别人的任务",
                                       "members": [{"id": "ou_someone_else", "role": "assignee"}]}))
    r = dispatch("task.task.update_user_access_v2", {})
    check(S, "分给别人的任务：不建", not by_key("feishu-task:sbx-task-guid-2"), r)


def scenario_idle():
    print("\n[7] 空闲时：定时检查不唤醒模型", flush=True)
    S = "空闲"
    import subprocess
    run = subprocess.run([sys.executable, "-m", "agent_matrix.precheck"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    out = (run.stdout or "").strip().splitlines()[-1:] or [""]
    check(S, "前置检查：没活就跳过（退出码 2，不叫模型）", run.returncode == 2, f"exit {run.returncode} {out[0][:200]}")
    job = drive.next_job(ROOT, CONFIG, "测试AI", kinds=["meeting", "chat"])
    check(S, "没有待提取的会议或聊天", job is None, job and job["id"])


def scenario_page(made):
    print("\n[8] 心响页面数据", flush=True)
    S = "页面"
    from agent_matrix import app
    page = app.build_state(ROOT)
    blob = json.dumps(page, ensure_ascii=False, default=str)
    ai = made.get("ai") or {}
    check(S, "页面数据能生成，含刚才的事项", f"渠道复盘报告" in blob and str(ai.get("n")) in blob, len(blob))


def scenario_polling():
    print("\n[10] 没有会议推送：监听每分钟的定时检查自己发现刚结束的会议", flush=True)
    S = "无推送轮询"
    from agent_matrix.state import State
    mid, note = "9000000000000000010", "sbx-note-10"
    start = (NOW - timedelta(minutes=45)).replace(microsecond=0)
    world(lambda w: (w["meetings"].__setitem__(mid, {"meeting_id": mid, "topic": "【测试】没推送的会", "start_time": start.isoformat(),
                                                     "end_time": (NOW - timedelta(minutes=3)).replace(microsecond=0).isoformat(),
                                                     "note_id": note}),
                     w["transcripts"].__setitem__(note, "[00:00:05] 测试\n"),
                     w.__setitem__("search", [mid])))

    def at(minutes_ago):
        state = State(ROOT / "state/state.db")
        try:
            state.set("listen:heartbeat", datetime.now(TZ).isoformat())
            state.set("listen:status", {"vc.meeting.participant_meeting_ended_v1": "not_subscribed",
                                        "vc.note.generated_v1": "not_subscribed", "im.message.receive_v1": "live"})
            state.set("meeting_watch:last_search", (datetime.now(TZ) - timedelta(minutes=minutes_ago)).isoformat())
        finally:
            state.close()
        return LISTENER.locked(lambda c, cl, s: LISTENER.meeting_check(c, cl, s, datetime.now(TZ)))

    r = at(2)
    check(S, "上次搜索才 2 分钟：不去搜，会议还没被发现", r == [] and meeting_row(mid) is None, r)
    r = at(6)
    check(S, "超过 5 分钟：自己搜到这场会，逐字稿已有就直接就绪", r == [mid] and meeting_row(mid)[0] == "ready", {"new": r, "row": meeting_row(mid)})
    r = at(6)
    check(S, "下一分钟再查：不重复报告、不重复排队", r == [] and meeting_row(mid)[0] == "ready", r)
    world(lambda w: w.__setitem__("search", []))


def scenario_modes():
    print("\n[11] 三种接收模式和手动新建", flush=True)
    S = "接收模式"
    from agent_matrix import manual
    from agent_matrix.config import save
    global CONFIG
    made = manual.add(ROOT, "【测试】手动新建的事", who="我", due="明天")
    row = next((i for i in items().values() if i["n"] == made["n"]), {})
    check(S, "手动新建：有编号、状态未开始、截止日算好", made["n"] > 0 and one(row.get("状态")) == "未开始" and row.get("截止日"), row.get("标题"))
    CONFIG = CONFIG.model_copy(update={"intake_mode": "manual"})
    save(ROOT / "config.yaml", CONFIG)
    world(lambda w: w["tasks"].append({"guid": "sbx-task-manual", "summary": "【测试】手动模式下来的任务", "url": "https://applink.feishu.cn/client/todo/detail?guid=sbx-task-manual",
                                       "members": [{"id": OWNER, "role": "assignee"}], "created_at": str(int(NOW.timestamp() * 1000))}))
    r = dispatch("task.task.update_user_access_v2", {})
    r2 = dispatch("vc.meeting.participant_meeting_ended_v1", {"meeting_id": "9000000000000000099"})
    check(S, "手动模式：飞书任务和会议推送都不接", r == "manual" and r2 == "manual" and not by_key("feishu-task:sbx-task-manual"), {"task": r, "meeting": r2})
    CONFIG = CONFIG.model_copy(update={"intake_mode": "accepted"})
    save(ROOT / "config.yaml", CONFIG)
    r = dispatch("task.task.update_user_access_v2", {})
    got = by_key("feishu-task:sbx-task-manual")
    from agent_matrix.thread import judgments
    check(S, "全自动模式：别人分来的任务先进「等你确认接不接」，不直接开工",
          len(got) == 1 and one(got[0].get("状态")) == "待确认" and judgments(ROOT).get(got[0]["record_id"], {}).get("decision") == "ask", r)
    CONFIG = CONFIG.model_copy(update={"intake_mode": "all"})
    save(ROOT / "config.yaml", CONFIG)


def lifecycle_and_errors():
    print("\n[9] 运行日志：状态流转与错误", flush=True)
    S = "运行日志"
    conn = db.read(ROOT / "state/activity.db")
    try:
        start = json.loads((ROOT / "live-before.json").read_text())["activity_max_id"] or 0
        rows = conn.execute("SELECT kind, status, title, detail FROM events WHERE id>? ORDER BY id", (start,)).fetchall()
    finally:
        conn.close()
    observed = [r for r in rows if r[0] == "lifecycle"]
    failed = [r for r in rows if r[1] == "failed" and "校验拦掉了" not in r[2]]  # that one is scenario 3 on purpose
    swallowed = [r for r in rows if r[1] == "swallowed"]
    check(S, "没有不在状态表里的流转", not observed, observed)
    check(S, "测试期间没有失败记录", not failed, failed)
    print(f"  · 已跳过的可选错误 {len(swallowed)} 条" + "".join(f"\n      {r[2]}：{r[3][:120]}" for r in swallowed[:6]))
    unsupported = [c["args"][:3] for c in fake_calls() if c["result"] != "ok"]
    check(S, "所有飞书调用都在模拟范围内", not [u for u in unsupported if "fake_unsupported" in str(u)], unsupported)


if __name__ == "__main__":
    made = {}
    for step in (scenario_meeting, scenario_quiet_meeting, scenario_bad_extraction):
        try:
            got = step()
            made.update(got or {})
        except Exception:
            check(step.__name__, "场景运行出错", False, traceback.format_exc()[-600:])
    for step in (scenario_ai_work, scenario_card):
        try:
            step(made)
        except Exception:
            check(step.__name__, "场景运行出错", False, traceback.format_exc()[-600:])
    for step in (scenario_feishu_task, scenario_idle, lambda: scenario_page(made), scenario_polling, scenario_modes, lifecycle_and_errors):
        try:
            step()
        except Exception:
            check(getattr(step, "__name__", "step"), "场景运行出错", False, traceback.format_exc()[-600:])
    (ROOT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    passed = sum(r["ok"] for r in results)
    print(f"\n通过 {passed}/{len(results)}")
    sys.exit(0 if passed == len(results) else 1)
