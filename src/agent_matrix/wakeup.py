"""心响 · 叫醒：有活时由心响主动去叫 AI，叫不醒就本机先顶上，并说清为什么没叫醒。

Nobody waits on the line. Work for an AI that lives in another app (Cindy, or any AI that said hello) waits in the
queue; this layer, run by the listener every two minutes, in code and without a model:

    1. is there work for that AI that nobody has taken?            drive.work_for (the same check as `gate`)
    2. wake it, if it gave 心响 a way to: a signed webhook          send_wake (Standard Webhooks; Cindy supports it)
    3. still not taken after GRACE: say why, and stand in         diagnose + the built-in driver takes its work with
                                                                  the fallback AI (设置 → 叫不醒时谁来顶上)
    4. it comes back (takes work, or its schedule runs fine): the stand-in ends by itself

The built-in driver reads `standing_in` (drive.local_kinds / item_filter) and the fallback AI (engines.engine).
"""
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess

from . import activity, thread as conversation
from .timeutil import TZ

GRACE = timedelta(minutes=8)    # work untaken this long after the first wake: the built-in driver stands in
REWAKE = timedelta(minutes=5)   # wake again at most this often while the work waits
STATE = "ai:wake"


def _now():
    return datetime.now(TZ)


def _get(root, key):
    return dict(conversation.setting(Path(root), key, None) or {})


def _put(root, key, value):
    conversation.set_setting(Path(root), key, value)


# ---- who needs waking -----------------------------------------------------------------------------------------

def driver_of(value):
    """The name an AI asks for work with: "Cindy" for Cindy's schedule, the hello name for an outside AI."""
    from .aidrivers import name_in
    return "Cindy" if value == "cindy" else name_in(value)


def chosen(root):
    """The AIs in other apps that the owner chose for doing items or for sorting (settings), each once."""
    out = []
    for key in ("engine", "engine_sort"):
        value = str(conversation.setting(Path(root), key, "") or "")
        if (value == "cindy" or value.startswith("driver:")) and value not in out:
            out.append(value)
    return out


def standing_in(root):
    """The AIs the built-in driver is standing in for right now: {value: {"since", "why", "engine", "work"}}."""
    from .aidrivers import standing_in as read
    return read(root)


def fallback(root):
    """The local AI that stands in (设置 → 叫不醒时谁来顶上); None when the owner switched standing in off."""
    from .engines import fallback_engine
    return fallback_engine(root)


# ---- waking: a signed webhook ---------------------------------------------------------------------------------

from .aidrivers import hook_of, new_secret, pending_secret, send_wake, set_hook, signed   # each AI's wake address


# ---- why it does not come --------------------------------------------------------------------------------------

def _app_running(name="Cindy"):
    try:
        return subprocess.run(["pgrep", "-f", f"{name}.app/Contents/MacOS/{name}"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True   # cannot tell: do not blame it


def _cindy_last_runs(limit=3):
    from contextlib import closing
    from .local_sessions import SCHEDULE_NAME, _cindy_db, _ro
    path = _cindy_db()
    if not path:
        return []
    with closing(_ro(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='schedule_runs'").fetchone():
            return []
        rows = db.execute("""SELECT r.fired_at, r.status, coalesce(r.error_msg, ''), coalesce(r.result_text, '')
            FROM schedule_runs r JOIN schedules s ON s.id = r.schedule_id WHERE s.name = ?
            ORDER BY r.fired_at DESC LIMIT ?""", (SCHEDULE_NAME, limit)).fetchall()
    return [{"at": datetime.fromtimestamp(int(f) / 1000, TZ), "status": st, "error": err, "said": said}
            for f, st, err, said in rows]


def _error_words(text):
    low = str(text or "").lower()
    if any(w in low for w in ("401", "unauthorized", "login", "logged in", "expired", "token", "auth")):
        return "它用的模型账号登录失效了（订阅掉了或要重新登录）"
    if any(w in low for w in ("429", "rate limit", "quota", "insufficient", "balance", "usage limit", "额度", "余额")):
        return "它用的模型额度用完了或被限流"
    if "not available" in low or "model" in low and "not found" in low:
        return "它选的模型现在用不了"
    if "timed out" in low or "timeout" in low or "超时" in low:
        return "上一轮跑超时了"
    return f"上一轮出错了：{str(text).strip()[:60]}"


def diagnose(root, value, at=None, since=None):
    """Why this AI has not taken its work, in words the owner can act on. Code only, read-only."""
    at = at or _now()
    if value != "cindy":
        from .aidrivers import known
        here = next((d for d in known(root) if d["name"] == driver_of(value)), None)
        if not here:
            return f"{driver_of(value)} 还没在心响报到过"
        return f"{here['name']} 从 {here['seen'] or '很久以前'} 起没来领活（它那段对话结束了，或它的定时任务停了）"
    from .local_sessions import CINDY_APP, cindy_schedule
    if not CINDY_APP.is_dir():
        return "这台电脑上没装 Cindy"
    if not _app_running():
        return "Cindy 没开着"
    try:
        schedule, runs = cindy_schedule(), _cindy_last_runs()
    except Exception as exc:
        activity.swallowed(Path(root), "wakeup.diagnose", exc)
        return "读不到 Cindy 的定时任务记录"
    if not schedule:
        return "Cindy 里没有「心响｜领活来做」这个定时任务"
    if schedule.get("status") != "active":
        return "Cindy 的「心响｜领活来做」被暂停了"
    if not runs or at - runs[0]["at"] > timedelta(minutes=12) or (since and runs[0]["at"] < since):
        return "Cindy 的定时任务在有活之后还没跑过（Mac 睡着过，或 Cindy 卡住了）"
    last = runs[0]
    if last["status"] == "running":
        return "Cindy 正在跑上一轮，还没轮到这件"
    if last["status"] in {"failed", "error"}:
        return "Cindy " + _error_words(last["error"] or last["said"])
    if last["status"] == "skipped":
        return "Cindy 来看过，但它的前置检查说没有它的活（设置和它那边对不上）"
    return "Cindy 跑了，但没有领这件活"


def came_back(root, value, since):
    """It is doing its work again: an outside AI asked for work since, or Cindy's schedule ran cleanly since."""
    if value != "cindy":
        from .aidrivers import known
        here = next((d for d in known(root) if d["name"] == driver_of(value)), None)
        return bool(here and here["online"])
    try:
        runs = _cindy_last_runs(limit=1)
    except Exception as exc:
        activity.swallowed(Path(root), "wakeup.came_back", exc)
        return False
    return bool(runs and runs[0]["at"] > since and runs[0]["status"] in {"skipped", "success", "succeeded", "done"})


# ---- the round the listener runs ------------------------------------------------------------------------------

def _taking(root, name):
    from .drive import open_jobs
    return any(str(j.get("driver") or "") == name for j in open_jobs(root))


def watch(root, config, at=None, *, work_for=None, wake=None, kick=None):
    """For every AI in another app: wake it when its work waits, stand in when it does not come, step back when
    it is here again. Returns what happened (for the log)."""
    from . import inbox
    from .drive import WORK_WORDS
    at, root = at or _now(), Path(root)
    if work_for is None:
        from .drive import work_for as check
        work_for = lambda name: check(root, config, name)
    wake = wake or send_wake
    state, stood, done = _get(root, STATE), standing_in(root), []
    for value in chosen(root):
        name, entry = driver_of(value), dict(state.get(value) or {})
        if value in stood and came_back(root, value, datetime.fromisoformat(stood[value]["since"])):
            stood.pop(value)
            inbox.add(root, f"standin-end:{value}:{at:%Y%m%d%H%M}", f"✓ {name} 回来了，它的活又交还给它做。", kind="system")
            done.append((name, "back"))
        if _taking(root, name):
            state.pop(value, None)
            continue
        work = work_for(name)
        if not work:
            state.pop(value, None)
            continue
        since = datetime.fromisoformat(entry["since"]) if entry.get("since") else at
        entry["since"] = since.isoformat(timespec="seconds")
        last = datetime.fromisoformat(entry["woke"]) if entry.get("woke") else None
        if value not in stood and (last is None or at - last >= REWAKE):
            result = wake(hook_of(root, value), work)
            entry.update(woke=at.isoformat(timespec="seconds"), wake=result)
            done.append((name, "woke" if result.get("ok") else "no-wake"))
        if value not in stood and at - since >= GRACE:
            why = diagnose(root, value, at, since)
            if not (entry.get("wake") or {}).get("ok") and (entry.get("wake") or {}).get("why"):
                why += "；" + entry["wake"]["why"]
            stand = fallback(root)
            words = WORK_WORDS.get(work, "活")
            if stand:
                from .engines import run_class
                stood[value] = {"since": at.isoformat(timespec="seconds"), "why": why, "engine": stand, "work": work}
                inbox.add(root, f"standin:{value}:{entry['since']}",
                          f"{run_class(stand).NAME} 先替 {name} 做了「{words}」。\n{name} 没来接：{why}。它恢复后会自动交还。",
                          kind="system")
                done.append((name, "standin"))
                (kick or _kick)(root, config)
            else:
                inbox.add(root, f"standin-none:{value}:{entry['since']}",
                          f"⚠ 「{words}」在等 {name}，叫不醒：{why}。\n设置里也没选本机顶上的 AI，这件活会一直等着。",
                          kind="system")
                done.append((name, "stuck"))
        state[value] = entry
    _put(root, STATE, state)
    from .aidrivers import STANDIN
    _put(root, STANDIN, stood)
    return done


def _kick(root, config):
    from .workenv import kick
    kick(root, config, force=True)


def status(root):
    """For the settings page: per AI in another app, how 心响 wakes it and whether someone stands in for it now."""
    from .aidrivers import HOOKS
    hooks, stood, state = _get(root, HOOKS), standing_in(root), _get(root, STATE)
    return {value: {"name": driver_of(value), "hook": bool((hooks.get(value) or {}).get("url")),
                    "standin": stood.get(value), "last_wake": (state.get(value) or {}).get("wake")} for value in chosen(root)}
