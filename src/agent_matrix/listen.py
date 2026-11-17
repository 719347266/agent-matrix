"""Always-on listener: Feishu pushes events here, plain code handles them at once. No model.

    python -m agent_matrix.listen            # runs forever (installed as a launchd agent)
    python -m agent_matrix.listen --status   # what is connected right now

Pushed events (one `lark-cli event consume` child each):
  im.message.receive_v1                    owner's replies to the bot → applied, ✓ within seconds
  vc.meeting.participant_meeting_ended_v1  meeting ended → queued immediately
  vc.note.generated_v1                     notes/transcript ready → marked ready; the 2-minute
                                           Cindy check then wakes the model for extraction
  task.task.update_user_access_v2          Feishu task changed → synced into the task table
Timers: prep cards ~30 min before meetings, brief catch-up, Friday review (every minute);
chat reading (every 10 min); Feishu task sync fallback (every 15 min).

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
          ("vc.note.generated_v1", "user"), ("task.task.update_user_access_v2", "user")]


def log(*parts):
    print(datetime.now(TZ).strftime("%m-%d %H:%M:%S"), *parts, flush=True)


class Consumer(threading.Thread):
    """Keeps one `lark-cli event consume` alive and forwards each event line."""

    def __init__(self, key, identity, inbox, status, binary):
        super().__init__(daemon=True)
        self.key, self.identity, self.inbox, self.status, self.binary = key, identity, inbox, status, binary

    def run(self):
        delay = 10
        while True:
            env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
            proc = subprocess.Popen([self.binary, "event", "consume", self.key, "--as", self.identity],
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
            state.set("listen:status", dict(self.status))
        finally:
            state.close()

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

    def on_meeting(self, config, client, state, event):
        from .meeting_watch import MeetingQueue, MeetingSource, ingest
        mid = event.get("meeting_id") or (event.get("note_source") or {}).get("source_entity_id")
        if not mid:
            return "no_meeting_id"
        return ingest(config, MeetingSource(client), MeetingQueue(state), str(mid), datetime.now(TZ))

    def on_task(self, config, client, state, event):
        from .sources import feishu_tasks
        return feishu_tasks.sync(config, client, state, datetime.now(TZ)) if config.feishu_tasks_enabled else "off"

    def dispatch(self, key, event):
        handler = {"im.message.receive_v1": self.on_message, "vc.note.generated_v1": self.on_meeting,
                   "vc.meeting.participant_meeting_ended_v1": self.on_meeting,
                   "task.task.update_user_access_v2": self.on_task}[key]
        try:
            result = self.locked(lambda c, cl, s: handler(c, cl, s, event))
            log("event", key, "→", json.dumps(result, ensure_ascii=False, default=str)[:160])
        except MatrixBusy:
            self.retry.append((time.time() + 15, key, event))
        except Exception as exc:  # one bad event must not stop the listener
            log("event failed", key, type(exc).__name__, str(exc)[:200])

    # ---- timers -----------------------------------------------------------------
    def every(self, name, seconds):
        now = time.time()
        if now - self.last.get(name, 0) >= seconds:
            self.last[name] = now
            return True
        return False

    def timers(self):
        jobs = []
        if self.every("minute", 60):
            def minute(config, client, state):
                from .agents import meeting_prep, weekly_review
                from .reply import catch_up_briefs
                now = datetime.now(TZ)
                out = {"prep": meeting_prep.run(config, client, state, now, horizon=31, lead=20)}
                if state.get("brief-v1:migration-stage") == "v1_active":
                    out["catch_up"] = catch_up_briefs(config, client, state, now)
                    out["weekly"] = weekly_review.run(config, client, state, now)
                return out
            jobs.append(("minute", minute))
        if self.every("chats", 600):
            def chat_poll(config, client, state):
                from .sources import chats
                if not config.chat_watch_enabled:
                    return "off"
                return chats.poll(config, client, state, datetime.now(TZ), skip_chat_ids=[state.get("inbox:chat_id")])
            jobs.append(("chats", chat_poll))
        if self.every("tasks", 900):
            def tasks(config, client, state):
                from .sources import feishu_tasks
                return feishu_tasks.sync(config, client, state, datetime.now(TZ)) if config.feishu_tasks_enabled else "off"
            jobs.append(("tasks", tasks))
        for name, job in jobs:
            try:
                result = self.locked(job)
                if name != "minute" or any(v for v in (result or {}).values()):
                    log("timer", name, "→", json.dumps(result, ensure_ascii=False, default=str)[:200])
            except MatrixBusy:
                self.last[name] = 0  # try again on the next loop
            except Exception as exc:
                log("timer failed", name, type(exc).__name__, str(exc)[:200])

    def run(self):
        config = load(self.path)
        for key, identity in EVENTS:
            Consumer(key, identity, self.inbox, self.status, config.lark_binary).start()
        log("listener started", self.root)
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
    Listener(path).run()


if __name__ == "__main__":
    raise SystemExit(main())
