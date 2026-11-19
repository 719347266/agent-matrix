"""Always-on listener: Feishu pushes events here, plain code handles them at once. No model.

    python -m agent_matrix.listen            # runs forever (installed as a launchd agent)
    python -m agent_matrix.listen --status   # what is connected right now

Pushed events (one `lark-cli event consume` child each):
  im.message.receive_v1                    owner's replies to the bot → applied, ✓ within seconds
  vc.meeting.participant_meeting_ended_v1  meeting ended → queued immediately
  vc.note.generated_v1                     notes/transcript ready → marked ready; the AI employee
                                           (worker.py) is woken at once for extraction
  task.task.update_user_access_v2          Feishu task changed → synced into the task table
  card.action.trigger                      a tapped card button → applied, the card turns into "✓ 已完成"
Timers: prep cards ~30 min before meetings, brief catch-up, Friday review (every minute);
chat reading (every 2 min); Feishu task sync fallback (every 15 min); the AI employee is
started every 2 minutes when it is not already working (it exits when nothing is left).

A subscription the Feishu console has not enabled yet is retried every 10 minutes, so
enabling it later needs no restart. Every job is idempotent with the 5-minute fallback poll.
"""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import queue as queuelib
import subprocess
import sys
import threading
import time
import traceback

from .bitable import js
from .config import load
from .feishu import Feishu
from .models import MatrixError, MatrixBusy
from .state import State, exclusive_lock
from .timeutil import TZ

EVENTS = [("im.message.receive_v1", "bot"), ("vc.meeting.participant_meeting_ended_v1", "user"),
          ("vc.note.generated_v1", "user"), ("task.task.update_user_access_v2", "user"),
          ("card.action.trigger", "bot")]


def log(*parts):
    print(datetime.now(TZ).strftime("%m-%d %H:%M:%S"), *parts, flush=True)


def backup_loop(root, path):
    """心响 keeps the items on this computer; this copies every change to the Feishu table in the background,
    in its own thread, so a slow or failing Feishu never holds up a click or another timer."""
    from . import itemstore
    client, wait = None, 3
    while True:
        time.sleep(wait)
        try:
            config = load(path)
            if not itemstore.enabled(config) or not itemstore.imported(root) or not itemstore.status(root)["pending"]:
                wait = 3
                continue
            if config.item_store in ("notion", "device"):
                from . import notion
                out = itemstore.backup(root, push=notion.push(config) if config.item_store == "notion" else lambda *a: None,
                                       target="Notion ")
                if out["pushed"] and config.item_store == "notion":
                    log("backup", out["pushed"], "→ Notion", "· 还剩", out["pending"])
                wait = 3 if out["pushed"] or not out["pending"] else 60
                continue
            if client is None:
                client = Feishu(config, root)
                client.use_local_items = False  # this one writes the Feishu table itself
                client.verify_owner(client.auth())
            out = itemstore.backup(root, client)
            if out["pushed"]:
                log("backup", out["pushed"], "→ 飞书", "· 还剩", out["pending"])
            wait = 3 if out["pushed"] or not out["pending"] else 60
        except Exception as exc:
            client, wait = None, 60
            log("backup failed", type(exc).__name__, str(exc)[:160])


class Consumer(threading.Thread):
    """Keeps one `lark-cli event consume` alive and forwards each event line."""

    def __init__(self, key, identity, inbox, status, binary):
        super().__init__(daemon=True)
        self.key, self.identity, self.inbox, self.status, self.binary = key, identity, inbox, status, binary

    def run(self):
        delay = 10
        while True:
            env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
            from .feishu import resolve_lark
            proc = subprocess.Popen([resolve_lark(self.binary), "event", "consume", self.key, "--as", self.identity],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, env=env)  # stdin stays open: EOF would end the stream
            errors = []
            def read_errors():
                for line in proc.stderr:
                    errors.append(line)
                    if "ready event_key" in line:
                        self.status[self.key] = "live"
            threading.Thread(target=read_errors, daemon=True).start()
            self.status[self.key] = "connecting"
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                self.status[self.key] = "live"
                delay = 10
                try:
                    self.inbox.put((self.key, json.loads(line)))
                except ValueError:
                    log("bad event line", self.key)
            proc.wait()
            text = "".join(errors)
            if "not subscribed in console" in text:
                self.status[self.key] = "not_subscribed"
                import re as _re
                link = _re.search(r"https://open\.feishu\.cn/page/launcher[^\s\"]+", text)
                if link:
                    self.status["link:" + self.key] = link.group(0)  # kept locally, shown by the monitor
                time.sleep(600)
                continue
            if "ready event_key" in text and proc.returncode == 0:
                self.status[self.key] = "reconnecting"
            else:
                self.status[self.key] = f"error {proc.returncode}"
                log("consumer stopped", self.key, text[-300:].replace("\n", " "))
            time.sleep(delay)
            delay = min(delay * 2, 300)


class Listener:
    def __init__(self, path):
        self.path, self.root = path, path.parent
        self.inbox = queuelib.Queue()
        self.status = {}
        self.retry = []
        self.last = {}
        self.fixed_done = {}

    def locked(self, job):
        """Run a job under the project lock; a busy lock means try again shortly."""
        config = load(self.path)
        with exclusive_lock(self.root / "state"):
            state = State(self.root / "state/state.db")
            try:
                client = Feishu(config, self.root)
                client.verify_owner(client.auth())
                return job(config, client, state)
            finally:
                state.close()

    def heartbeat(self):
        state = State(self.root / "state/state.db")
        try:
            state.set("listen:heartbeat", datetime.now(TZ).isoformat())
            state.set("listen:status", {k: v for k, v in self.status.items() if not k.startswith("link:")})
            state.set("listen:links", {k[5:]: v for k, v in self.status.items() if k.startswith("link:")})
        finally:
            state.close()
        from . import activity
        activity.beat(self.root, "实时监听", " / ".join(f"{k.split('.')[0]}:{v}" for k, v in self.status.items()))

    # ---- event handlers -------------------------------------------------------
    def on_message(self, config, client, state, event):
        from .reply import handle
        from .delivery import send_once
        from hashlib import sha256
        sender = event.get("sender_id") or {}
        sender = sender.get("open_id") if isinstance(sender, dict) else sender
        mid = event.get("message_id") or event.get("id")
        if event.get("chat_type") != "p2p" or sender != config.owner_open_id or not mid:
            return "ignored"
        if state.get("inbox:seen:" + mid):
            return "seen"
        body = event.get("content") or ""
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
            text = parsed.get("text", "") if isinstance(parsed, dict) else str(body)
        except ValueError:
            text = str(body)
        if event.get("chat_id"):
            state.set("inbox:chat_id", event["chat_id"])
        reply = handle(config, client, state, text, source="feishu", message_ref=mid) if text.strip() else None
        if reply:
            send_once(state, "reply-ack:" + mid, lambda: client.send_self(reply, "ack-" + sha256(mid.encode()).hexdigest()[:32]))
        state.set("inbox:seen:" + mid, True)
        return "handled" if reply else "not_a_command"

    def on_card(self, config, client, state, event):
        """A「✓ 完成」/「✓ 通过」button on a card: run its command like a typed reply, then confirm."""
        from .reply import handle
        from .delivery import send_once
        from hashlib import sha256
        if event.get("operator_id") != config.owner_open_id or event.get("action_tag") != "button":
            return "ignored"
        try:
            value = json.loads(event.get("action_value") or "{}")
        except ValueError:
            value = {}
        cmd = value.get("cmd") if isinstance(value, dict) else None
        eid = event.get("event_id") or ""
        if not cmd or not eid:
            return "no_command"
        if state.get("card:seen:" + eid):
            return "seen"
        # The test card's button ("给我发张测试卡片"): changes nothing, only proves taps arrive.
        reply = "✓ 按钮可用" if cmd == "__ping__" else handle(config, client, state, cmd, source="card", message_ref=eid)
        # The card itself changes ("✓ 已完成 14:52"); a chat message only when the card cannot be updated.
        from .cards import update_in_place
        in_place = bool(reply) and update_in_place(client, client.root, event, cmd, reply, datetime.now(TZ))
        if reply and not in_place:
            send_once(state, "card-ack:" + eid, lambda: client.send_self(reply, "card-" + sha256(eid.encode()).hexdigest()[:32]))
        state.set("card:seen:" + eid, True)
        return "updated" if in_place else "handled"

    def on_meeting(self, config, client, state, event):
        from .meeting_watch import MeetingQueue, MeetingSource, ingest
        mid = event.get("meeting_id") or (event.get("note_source") or {}).get("source_entity_id")
        if not mid:
            return "no_meeting_id"
        result = ingest(config, MeetingSource(client), MeetingQueue(state), str(mid), datetime.now(TZ))
        self.wake_worker(config, force=True)
        return result

    def on_task(self, config, client, state, event):
        from .sources import feishu_tasks
        return feishu_tasks.sync(config, client, state, datetime.now(TZ)) if config.feishu_tasks_enabled else "off"

    def dispatch(self, key, event):
        handler = {"im.message.receive_v1": self.on_message, "vc.note.generated_v1": self.on_meeting,
                   "vc.meeting.participant_meeting_ended_v1": self.on_meeting,
                   "task.task.update_user_access_v2": self.on_task, "card.action.trigger": self.on_card}[key]
        try:
            result = self.locked(lambda c, cl, s: handler(c, cl, s, event))
            log("event", key, "→", json.dumps(result, ensure_ascii=False, default=str)[:160])
        except MatrixBusy:
            self.retry.append((time.time() + 15, key, event))
        except Exception as exc:  # one bad event must not stop the listener
            log("event failed", key, type(exc).__name__, str(exc)[:200])
            from . import activity
            activity.log(self.root, "error", f"实时事件处理出错：{key}", detail=str(exc), status="failed")

    # ---- timers -----------------------------------------------------------------
    def every(self, name, seconds):
        now = time.time()
        if now - self.last.get(name, 0) >= seconds:
            self.last[name] = now
            return True
        return False

    def wake_worker(self, config, force=False):
        """The AI employee runs in its own process; this only starts it when it is idle."""
        try:
            from .workenv import kick
            if kick(self.root, config, force=force) == "started":
                log("worker", "started")
        except Exception as exc:
            log("worker kick failed", type(exc).__name__, str(exc)[:200])

    def meeting_check(self, config, client, state, now):
        """Runs every minute: finds finished meetings on the meeting_watch schedule (every 5 minutes while
        Feishu does not push meeting events) and starts the AI employee as soon as a transcript is ready.
        Returns the meetings that just became ready (empty when nothing happened, so the log stays quiet).
        A failure here is recorded and never stops the other minute jobs."""
        if not config.meeting_watch_enabled:
            return None
        from .meeting_watch import MeetingQueue, MeetingSource, check
        from . import activity
        queue = MeetingQueue(state)
        before = {r["meeting_id"]: r["status"] for r in queue.list()}
        try:
            check(config, MeetingSource(client), queue, now)
        except Exception as exc:
            activity.log(self.root, "error", "会议检查出错", detail=f"{type(exc).__name__}: {exc}"[:300], status="failed")
            return None
        rows = queue.list()
        activity.beat(self.root, "会议检查", f"待处理 {sum(r['status'] in {'ready', 'prepared', 'needs_attention'} for r in rows)} 场")
        new = [r["meeting_id"] for r in rows if r["status"] in {"ready", "needs_attention"} and before.get(r["meeting_id"]) != r["status"]]
        if new:
            self.wake_worker(config, force=True)
        return new

    SCHEDULE = [("recurring", (6, 0), False), ("morning", (8, 30), True), ("evening", (21, 0), True)]

    def fixed_times(self):
        """06:00 周期事项, workday 08:30 早报 and 21:00 晚报, run here so no other scheduler is needed.

        Each runs once a day (also safe next to another scheduler: the brief keys are per date)."""
        now = datetime.now(TZ)
        for job, (h, m), workdays in self.SCHEDULE:
            if workdays and now.weekday() >= 5:
                continue
            if (now.hour, now.minute) < (h, m) or (now.hour, now.minute) > (h + 2, m):
                continue  # only within two hours of the slot; later catch-up is done by the brief itself
            key = f"listen:fixed:{job}:{now.date().isoformat()}"
            if self.fixed_done.get(key):
                continue
            self.fixed_done[key] = True
            def go(job=job):
                done = subprocess.run([sys.executable, "-m", "agent_matrix.scheduled", job], cwd=self.root, capture_output=True,
                                      text=True, encoding="utf-8", timeout=600)
                log("fixed", job, "→", (done.stdout or done.stderr or "").strip()[-160:])
            threading.Thread(target=go, daemon=True).start()

    def timers(self):
        jobs = []
        from .itemstore import feishu_free
        no_feishu = feishu_free(load(self.path))  # Feishu meetings, chats and tasks need Feishu; Slack does not
        if self.every("minute", 60) and not no_feishu:
            def minute(config, client, state):
                from .agents import meeting_prep, weekly_review
                from .reply import catch_up_briefs
                now = datetime.now(TZ)
                out = {"prep": meeting_prep.run(config, client, state, now, horizon=31, lead=20)}
                if state.get("brief-v1:migration-stage") == "v1_active":
                    out["catch_up"] = catch_up_briefs(config, client, state, now)
                    out["weekly"] = weekly_review.run(config, client, state, now)
                from . import monitor
                out["cindy_alert"] = monitor.alert_if_cindy_stalled(self.root, client, state, now)
                out["meetings"] = self.meeting_check(config, client, state, now)
                return out
            jobs.append(("minute", minute))
        if self.every("snapshot", 120):
            def snapshot(config, client, state):
                from .workflow import Gateway
                from . import monitor
                rows = Gateway(client, state).rows()
                monitor.save_snapshot(self.root, rows)
                return {"rows": len(rows)}
            jobs.append(("snapshot", snapshot))
        if self.every("chats", 120) and not no_feishu:
            def chat_poll(config, client, state):
                from .sources import chats
                if not config.chat_watch_enabled:
                    return "off"
                return chats.poll(config, client, state, datetime.now(TZ), skip_chat_ids=[state.get("inbox:chat_id")])
            jobs.append(("chats", chat_poll))
        if self.every("slack", 60):  # search: ~2 calls per poll, well under Slack limits
            def slack_poll(config, client, state):
                from .sources import slack
                if not config.chat_watch_enabled:
                    return "off"
                out = slack.poll(config, self.root, state, datetime.now(TZ))
                if isinstance(out, dict) and out.get("candidates"):
                    self.wake_worker(config)  # don't wait for the 2-minute kick; the worker decides if it's time
                return out
            jobs.append(("slack", slack_poll))
        if self.every("tasks", 900) and not no_feishu:
            def tasks(config, client, state):
                from .sources import feishu_tasks
                return feishu_tasks.sync(config, client, state, datetime.now(TZ)) if config.feishu_tasks_enabled else "off"
            jobs.append(("tasks", tasks))
        if self.every("worker", 120):
            self.wake_worker(load(self.path))
        if self.every("schedule", 60):
            self.fixed_times()
        if self.every("memory", 600):
            try:  # 未决: waiting items whose date has come (local only, no model)
                from . import memory
                told = memory.waiting_due(self.root)
                if told:
                    log("memory", "waiting due", told)
            except Exception as exc:
                log("memory failed", type(exc).__name__, str(exc)[:200])
        for name, job in jobs:
            try:
                result = self.locked(job)
                if name != "minute" or any(v for v in (result or {}).values()):
                    log("timer", name, "→", json.dumps(result, ensure_ascii=False, default=str)[:200])
            except MatrixBusy:
                self.last[name] = 0  # try again on the next loop
            except Exception as exc:
                log("timer failed", name, type(exc).__name__, str(exc)[:200])
                from . import activity
                activity.log(self.root, "error", f"监听定时任务出错：{name}", detail=str(exc), status="failed")

    def run(self):
        config = load(self.path)
        from .itemstore import feishu_free
        for key, identity in [] if feishu_free(config) else EVENTS:
            Consumer(key, identity, self.inbox, self.status, config.lark_binary).start()
        from .app import start_background
        served = start_background(self.root, config)
        threading.Thread(target=backup_loop, args=(self.root, self.path), daemon=True, name="xinxiang-backup").start()
        log("listener started", self.root, "· 心响", f"http://127.0.0.1:{config.app_port}/" if served else "(port busy, served elsewhere)")
        while True:
            try:
                key, event = self.inbox.get(timeout=5)
                self.dispatch(key, event)
            except queuelib.Empty:
                pass
            due = [r for r in self.retry if r[0] <= time.time()]
            self.retry = [r for r in self.retry if r[0] > time.time()]
            for _, key, event in due:
                self.dispatch(key, event)
            self.timers()
            if self.every("heartbeat", 60):
                try:
                    self.heartbeat()
                except Exception as exc:
                    log("heartbeat failed", type(exc).__name__)


def main(argv=None):
    p = argparse.ArgumentParser(description="实时监听飞书事件（纯代码，不调用模型）")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--status", action="store_true")
    args = p.parse_args(argv)
    path = args.config.resolve()
    if args.status:
        state = State(path.parent / "state/state.db")
        try:
            print(js({"heartbeat": state.get("listen:heartbeat"), "events": state.get("listen:status")}))
        finally:
            state.close()
        return 0
    if sys.stdout is None:  # started without a console (pythonw on Windows): log to a file
        (path.parent / "logs").mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(path.parent / "logs/listen.log", "a", buffering=1, encoding="utf-8")
    Listener(path).run()


if __name__ == "__main__":
    raise SystemExit(main())
