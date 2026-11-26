"""交付规格: for each kind of deliverable (需求文档, 方案, 周报 …), how this owner's ones are done well, and how sure we are.

    state(root, kind)      还没有 / 草稿 / 试用中 / 稳定 / 待复核, worked out from the owner's verdicts (memory.outcomes)
    plan(root, rid, kind)  what this round does: stop at an outline first, review in a round of its own, bring the expert view
    form / propose / save  the AI forms a first draft; a spec under review gets a proposed revision the owner accepts;
                           the owner's own edit is saved as is

The expert view (prompts/specs/<kind>.md, built in, the same for every user) is brought only when a spec is first
formed, when it is due for review, and in 仔细 mode: once a kind is stable, work just follows the owner's own spec.
Nothing here is about one particular owner: their spec lives in their own state/specs.
"""
from datetime import datetime, timedelta
from pathlib import Path
import json
import re

from .timeutil import TZ

TYPES = ("需求文档", "方案", "调研报告", "周报", "会议材料", "回复草稿", "数据汇总")
BIG = ("需求文档", "方案", "调研报告")          # worth an outline the owner looks at first
ROLE = {"需求文档": "产品经理", "方案": "方案负责人", "调研报告": "研究员", "周报": "项目负责人", "会议材料": "会议组织者",
        "回复草稿": "沟通顾问", "数据汇总": "数据分析师"}
LABELS = {"none": "还没有", "draft": "草稿", "trial": "试用中", "stable": "稳定", "review": "待复核"}
MODES = {"fast": "快速", "auto": "平衡", "careful": "仔细"}
# 做事方式 is three choices put together; the three named ways are ready-made mixes, any other mix is 自定义
STYLE = {
    "start": {"auto": "直接开工", "ask": "先问我"},              # an item the AI judged it can do: start, or wait for 「交给 AI」
    "outline": {"never": "不给提纲", "new": "新类型先给提纲", "always": "每件先给提纲"},   # 调研 + 提纲, then wait for the owner
    "review": {"list": "对照清单自查", "new": "新类型多审一轮", "always": "每件多审一轮"},   # a seasoned reader's round of its own
}
PRESETS = {"fast": {"start": "auto", "outline": "never", "review": "list"},
           "auto": {"start": "auto", "outline": "new", "review": "new"},
           "careful": {"start": "ask", "outline": "always", "review": "always"}}
RECENT, STABLE_PASSES, REVIEW_BADS, REVIEW_DAYS, REVIEW_USES = 5, 4, 2, 30, 10
GUESS = [("需求文档", r"需求|PRD|prd|功能规则|产品规则"), ("调研报告", r"调研|研究|竞品|分析报告|摸底"),
         ("周报", r"周报|周总结|周汇报|月报"), ("会议材料", r"会议材料|议程|会前|评审材料|汇报材料"),
         ("回复草稿", r"回复|回信|答复|告诉|通知|说明给"), ("数据汇总", r"数据|汇总|统计|报表|盘点|指标"),
         ("方案", r"方案|规划|计划|策略|思路|设计|框架")]


def _now():
    return datetime.now(TZ)


def folder(root):
    return Path(root) / "state" / "specs"


def _path(root, kind, suffix=".md"):
    if kind not in TYPES:
        raise ValueError("只有这几类有交付规格：" + "、".join(TYPES))
    return folder(root) / f"{kind}{suffix}"


def text(root, kind):
    try:
        return _path(root, kind).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def proposed(root, kind):
    try:
        return _path(root, kind, ".proposed.md").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def expert(root, kind):
    """What a seasoned person in this field includes and checks. Built in, the same for every user."""
    from . import promptfile
    try:
        return promptfile.read(root, f"specs/{kind}.md").strip()
    except OSError:
        return ""


def _meta(root, kind):
    from .thread import setting
    value = setting(Path(root), f"spec:{kind}", {}) or {}
    return value if isinstance(value, dict) else {}


def _set_meta(root, kind, **changes):
    from .thread import set_setting
    set_setting(Path(root), f"spec:{kind}", {**_meta(root, kind), **changes})


def guess(*texts):
    """The kind of deliverable a piece of work asks for, from its words; "" when none fits."""
    words = " ".join(str(t or "") for t in texts)
    return next((kind for kind, pattern in GUESS if re.search(pattern, words)), "")


def verdicts(root, kind):
    from . import memory
    try:
        return [o for o in memory.outcomes(Path(root)) if o.get("template") == kind]
    except Exception:
        return []


def _when(value):
    try:
        t = datetime.fromisoformat(str(value))
        return t if t.tzinfo else t.replace(tzinfo=TZ)
    except (TypeError, ValueError):
        return None


def state(root, kind, now=None):
    """{"state", "label", "uses", "passed", "recent", "formed", "reviewed", "why"} for one kind."""
    now = now or _now()
    meta = _meta(root, kind)
    base = {"kind": kind, "uses": 0, "passed": 0, "recent": 0, "formed": meta.get("formed", ""), "reviewed": meta.get("reviewed", ""),
            "proposed": bool(proposed(root, kind)), "why": ""}
    if not text(root, kind):
        return {**base, "state": "none", "label": LABELS["none"]}
    since = meta.get("reviewed") or meta.get("formed") or ""
    all_v = [v for v in verdicts(root, kind)]
    # verdicts from before the spec was last formed or reviewed judged an older spec: they no longer count
    vs = [v for v in all_v if not since or str(v.get("at", "")) >= str(since)[5:16].replace("T", " ")] if since else all_v
    recent = [v["verdict"] for v in vs[-RECENT:]]
    passed, bad = recent.count("通过"), sum(1 for v in recent if v != "通过")
    out = {**base, "uses": len(vs), "passed": passed, "recent": len(recent)}
    was_stable = any([v["verdict"] for v in vs[i:i + RECENT]].count("通过") >= STABLE_PASSES for i in range(max(0, len(vs) - RECENT + 1)))
    if not vs:
        return {**out, "state": "draft", "label": LABELS["draft"], "why": "还没被评价过"}
    if was_stable and bad >= REVIEW_BADS:
        return {**out, "state": "review", "label": LABELS["review"], "why": f"最近 {len(recent)} 次里有 {bad} 次没一次通过"}
    if len(recent) >= RECENT and passed >= STABLE_PASSES:
        last = _when(since)
        if last and now - last >= timedelta(days=REVIEW_DAYS):
            return {**out, "state": "review", "label": LABELS["review"], "why": f"已经 {REVIEW_DAYS} 天没复核"}
        if len(vs) >= REVIEW_USES and since:
            return {**out, "state": "review", "label": LABELS["review"], "why": f"复核后又用了 {len(vs)} 次"}
        return {**out, "state": "stable", "label": LABELS["stable"]}
    return {**out, "state": "trial", "label": LABELS["trial"]}


def overview(root):
    return [state(root, k) for k in TYPES]


# ---- 做事方式: 自动 (by how settled the spec is) / 快速 / 仔细; one for everything, and one item may differ --------

def style(root):
    """The owner's way of working: {start, outline, review, preset} (preset: fast / auto / careful, or custom)."""
    from .thread import setting
    value = setting(Path(root), "work_style", None)
    if not isinstance(value, dict):   # before the three choices: the named way chosen then
        old = setting(Path(root), "work_mode", "auto")
        value = PRESETS.get(old, PRESETS["auto"])
    out = {k: value.get(k) if value.get(k) in opts else PRESETS["auto"][k] for k, opts in STYLE.items()}
    return {**out, "preset": next((name for name, p in PRESETS.items() if p == out), "custom")}


def set_style(root, **choices):
    """Change some of the three choices; the rest stay."""
    from .thread import set_setting
    now = {k: v for k, v in style(root).items() if k in STYLE}
    for k, v in choices.items():
        if k not in STYLE or v not in STYLE[k]:
            raise ValueError(f"「{k}」没有「{v}」这个选项。")
        now[k] = v
    set_setting(Path(root), "work_style", now)
    return style(root)


def describe(st):
    """「直接开工 · 新类型先给提纲 · 新类型多审一轮」"""
    return " · ".join(STYLE[k][st[k]] for k in STYLE)


def mode(root, rid=None):
    """The named way for one item (its own if the owner set one), else the owner's: fast / auto / careful / custom."""
    from .thread import setting
    own = (setting(Path(root), "work_mode:items", {}) or {}).get(rid) if rid else None
    if own in MODES:
        return own
    return style(root)["preset"]


def item_style(root, rid=None):
    from .thread import setting
    own = (setting(Path(root), "work_mode:items", {}) or {}).get(rid) if rid else None
    return dict(PRESETS[own]) if own in MODES else {k: v for k, v in style(root).items() if k in STYLE}


def set_mode(root, value, rid=None):
    """value: fast / auto / careful (one of the ready-made mixes); for one item, "" goes back to the owner's way."""
    from .thread import set_setting, setting
    if rid:
        items = dict(setting(Path(root), "work_mode:items", {}) or {})
        if value in MODES:
            items[rid] = value
        elif value == "":
            items.pop(rid, None)
        else:
            raise ValueError("做事方式只有：快速、平衡、仔细。")
        set_setting(Path(root), "work_mode:items", items)
        return
    if value not in MODES:
        raise ValueError("做事方式只有：快速、平衡、仔细。")
    set_setting(Path(root), "work_mode", value)
    set_setting(Path(root), "work_style", dict(PRESETS[value]))


def plan(root, rid, kind, outline_done=False):
    """What this round does. outline: stop at 调研 + 提纲 and wait for the owner; review: a round of its own after the draft
    (else a check against the list in the same round); expert: bring the built-in expert view; form / revise: write the
    spec (first time) or a proposed revision (under review)."""
    how, st = mode(root, rid), item_style(root, rid)
    s = state(root, kind) if kind else {"state": "none", "label": LABELS["none"]}
    unsettled = s["state"] in ("none", "draft", "review")
    outline = st["outline"] == "always" or (st["outline"] == "new" and kind in BIG and unsettled)
    review = st["review"] == "always" or (st["review"] == "new" and bool(kind) and s["state"] != "stable")
    return {"mode": how, "mode_label": MODES.get(how, "自定义"), "kind": kind, "state": s["state"], "state_label": s["label"],
            "outline": outline and not outline_done, "review": review, "check": True,
            "expert": bool(kind) and (s["state"] in ("none", "review") or st["review"] == "always"),
            "form": bool(kind) and s["state"] == "none", "revise": bool(kind) and s["state"] == "review"}


# ---- writing specs --------------------------------------------------------------------------------------------

def form(root, kind, body):
    """The AI's first spec for a kind that had none: it is used from now on, as a draft the owner can read and edit."""
    body = str(body or "").strip()
    if kind not in TYPES or len(body) < 60 or text(root, kind):
        return False
    folder(root).mkdir(parents=True, exist_ok=True)
    _path(root, kind).write_text(body + "\n", encoding="utf-8")
    _set_meta(root, kind, formed=_now().isoformat(timespec="seconds"), by="AI")
    return True


def propose(root, kind, body):
    """A revision for a spec under review: it waits for the owner's 用."""
    body = str(body or "").strip()
    if kind not in TYPES or len(body) < 60 or not text(root, kind) or body == text(root, kind):
        return False
    _path(root, kind, ".proposed.md").write_text(body + "\n", encoding="utf-8")
    return True


def accept(root, kind):
    new = proposed(root, kind)
    if not new:
        return False
    _path(root, kind).write_text(new + "\n", encoding="utf-8")
    _path(root, kind, ".proposed.md").unlink(missing_ok=True)
    _set_meta(root, kind, reviewed=_now().isoformat(timespec="seconds"))
    return True


def drop(root, kind):
    """The owner does not want the revision: the spec stays, and counts as looked at."""
    try:
        _path(root, kind, ".proposed.md").unlink(missing_ok=True)
    except ValueError:
        return False
    _set_meta(root, kind, reviewed=_now().isoformat(timespec="seconds"))
    return True


def save(root, kind, body):
    """The owner's own edit: saved as is, and the spec counts as reviewed now."""
    body = str(body or "").strip()
    folder(root).mkdir(parents=True, exist_ok=True)
    if not body:
        _path(root, kind).unlink(missing_ok=True)
        return
    _path(root, kind).write_text(body + "\n", encoding="utf-8")
    now = _now().isoformat(timespec="seconds")
    _set_meta(root, kind, reviewed=now, **({} if _meta(root, kind).get("formed") else {"formed": now}))


def checklist(body):
    """The lines under 「## 检查清单」 of a spec (or the expert view)."""
    m = re.search(r"^##\s*检查清单\s*\n(.*?)(?=^##\s|\Z)", str(body or ""), re.M | re.S)
    return [re.sub(r"^[-*\d.、)\s]+", "", ln).strip() for ln in (m.group(1).splitlines() if m else []) if ln.strip().lstrip("-*0123456789.、) ")]


WRITING = "写作自审.md"


def writing_check(root, workdir):
    """The Chinese writing checklist (built in, from an open-source project: specs/LICENSES.md), put in the folder for
    the review: it is long, so the AI reads it when it reviews instead of carrying it in every prompt."""
    from . import promptfile
    try:
        (Path(workdir) / WRITING).write_text(promptfile.read(root, f"specs/{WRITING}"), encoding="utf-8")
        return True
    except OSError:
        return False


def keep_plan(workdir, p):
    (Path(workdir) / ".plan.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")


def read_plan(workdir):
    try:
        return json.loads((Path(workdir) / ".plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---- what the AI is told about this round ----------------------------------------------------------------------

SPEC_FORMAT = "格式：`## 结构`（分哪几节、每节写什么）/ `## 写法`（篇幅、语气、用词、文档放哪怎么命名）/ `## 要求`（本人说过的）/ `## 检查清单`（交付前逐条核对的点）/ `## 范例`（最像的两三份文档，链接）"


def work_text(root, p):
    """The section of the instructions that says how this round goes (p: plan())."""
    kind, role = p.get("kind") or "", ROLE.get(p.get("kind") or "", "")
    head = (f"## 这件事怎么做（做事方式：{p['mode_label']}；类型：{kind}；这一类的交付规格：{p['state_label']}）" if kind else
            f"## 这件事怎么做（做事方式：{p['mode_label']}；类型：程序没认出来，按完成标准写，结果里 template 写最贴近的一类或「其他」）")
    out = [head]
    spec = text(root, kind) if kind else ""
    if spec:
        out += ["", "### 本人这一类交付的规格（写法和要求，照着做）", spec]
    if p.get("expert") and kind:
        out += ["", f"### 资深{role}会怎么做（产品内置的专家视角；和上面的规格冲突时以规格为准）", expert(root, kind)]
    out += ["", "### 这一轮要交什么"]
    if p.get("outline"):
        out += ["先调研、列提纲，这一轮不写正文，交给本人看过再写：",
                f"1. 调研：读完这件事的材料；找本人团队写过的同类文档（照「本人是谁」和经验库里记的地方找，找不到按关键词搜）；想清楚资深{role or '的人'}做这件事会抓住哪几点。",
                "2. 写 `提纲.md`：`## 调研`（找到的同类文档和资料，每条带链接；这件事要抓住的几点）、`## 提纲`（每一节写什么，一两句话）、`## 要你定的`（缺的信息和你的建议；没有就写「无」）。"]
        if p.get("form"):
            out.append(f"3. 同时写 `规格.md`：这一类交付以后都照着做的规格。从上面的专家视角出发，按本人团队同类文档的实际写法改；只写通用的做法，不写这一件事的内容。{SPEC_FORMAT}。")
        if p.get("revise"):
            out.append(f"3. 这一类的规格到了复核的时候：同时写 `规格.md`，整份写出修订后的规格（结合这一类最近被退回的原因、本人的新要求、同类文档现在的写法），本人确认后才生效。{SPEC_FORMAT}。")
        out.append('4. `结果.json` 写 `{"outcome": "outline", "summary": "40字内：提纲要点", "question": "28字内：要本人先定的一件事；没有就写「按这个提纲写吗？」"}`，然后结束。')
        return "\n".join(out)
    if (Path(p.get("workdir") or ".") / "提纲.md").is_file():
        out.append("本人已经看过 `提纲.md`：照提纲写正文；本人对提纲的意见在任务单和对话里，以那里为准。")
    out.append("写正文，交 `交付.md` 和 `结果.json`（见下面的说明）。")
    if p.get("form"):
        out.append(f"另写 `规格.md`：这一类交付以后都照着做的规格（只写通用做法）。{SPEC_FORMAT}。")
    if p.get("revise"):
        out.append(f"这一类的规格到了复核的时候：另写 `规格.md`，整份写出修订后的规格，本人确认后才生效。{SPEC_FORMAT}。")
    if p.get("review"):
        out.append("写完先交；程序会请你换一个人的眼光单独再审一轮，审完改好再交。")
    elif p.get("check"):
        out.append(f"交之前对照检查清单逐条自查，语言按 `{WRITING}` 查一遍，把结论写进 `自审.md`：`## 自审`，每条一行，写「✓」或改了什么。")
    return "\n".join(out)


def review_request(p):
    """The round of its own after the draft: the same AI, now reading it as a seasoned person who did not write it."""
    role = ROLE.get(p.get("kind") or "", "同行")
    items = p.get("checklist") or []
    lines = "\n".join(f"- {x}" for x in items) or "- 会上定下的、本人说过的要求都写进去了\n- 没有凭空编的内容\n- 没参加会的人读完能看懂、能用"
    return (f"现在换一个人来审：你是资深{role}，第一次读这份 交付.md，挑它的毛病。逐条检查：\n{lines}\n"
            "- 对照简报：会上定下的、本人后来的要求，有没有漏的\n- 有没有没依据的规则、数字、例子\n"
            f"- 语言：按当前文件夹的 {WRITING}（中文写作检查表）查一遍，去掉空话、套话和模板化表达，不增加没有的事实\n"
            "把每一条的结论写进 自审.md（`## 自审`，每条一行：「✓」或「改了：……」），按审出的问题改好 交付.md 和 结果.json，再交。")
