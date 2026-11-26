"""Is this the same subject? Character-pair similarity on titles, no AI involved."""
import re

STRONG = 0.72
POSSIBLE = 0.45
VERBS = re.compile(r"^(备材料|准备|撰写|整理|输出|完成|补齐|细化|跟进|核对|确认|优化|安排|推进)[：:]?")
NOISE = re.compile(r"撰写|准备|整理|输出|补齐|细化|完成|跟进|核对|优化|并取得|取得|和|与|及|的|[\s（）()【】\[\]：:，,、。|｜/·\-]")


def subject(title):
    t = VERBS.sub("", str(title or "").strip())
    return NOISE.sub("", t)


def pairs(text):
    return {text[i:i + 2] for i in range(len(text) - 1)}


def similarity(a, b):
    """Share of character pairs in common, relative to the shorter subject (0..1)."""
    pa, pb = pairs(subject(a)), pairs(subject(b))
    if len(pa) < 2 or len(pb) < 2:
        return 0.0
    return len(pa & pb) / min(len(pa), len(pb))
