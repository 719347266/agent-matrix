def field(name, kind="text", **kwargs):
    return {"name": name, "type": kind, **kwargs}


def select(name, choices):
    return field(name, "select", multiple=False, options=[{"name": x} for x in choices])


def date(name, kind="datetime"):
    return field(name, kind, style={"format": "yyyy-MM-dd HH:mm"})


TABLES = {
    "事项表": [
        field("标题"), select("类型", ["会议待办", "周期事项", "临时", "会前准备"]),
        select("方向", ["我的", "别人答应我的", "别人的"]), field("负责人", "user", multiple=False),
        date("截止日"), date("首次截止日"), field("延期次数", "number", default_value=0),
        field("下一步动作"), select("状态", ["待确认", "待办", "进行中", "阻塞", "草稿已备", "完成", "取消"]),
        select("优先级", ["P0", "P1", "P2"]), field("预计耗时(分钟)", "number"),
        field("能否AI预做", "checkbox"), field("AI草稿链接", style={"type": "url"}),
        select("来源", ["会议", "周期规则", "手动"]), field("来源链接", style={"type": "url"}),
        field("原话引用", description="会议待办必填；保留逐字原话及时间戳，不使用 AI 纪要代替。"),
        field("去重键"), date("创建时间", "created_at"), date("更新时间", "updated_at"),
    ],
    "周期规则表": [
        field("名称"), field("频率", description="RRULE；示例留空，确认后填写。"), field("提前天数", "number"),
        field("默认负责人", "user", multiple=False), field("预计耗时(分钟)", "number"),
        field("检查清单"), field("能否AI预做", "checkbox"), field("预做指令"), field("启用", "checkbox"),
    ],
    "会议处理记录表": [
        field("会议ID"), field("日程ID"), field("标题"), date("开始时间"), date("结束时间"),
        field("参会人", "user", multiple=True), select("逐字稿状态", ["未就绪", "已获取", "无录制"]),
        select("提取状态", ["待处理", "已完成", "失败"]), field("提取待办数", "number"),
        field("准备卡片已发", "checkbox"), field("GPT纪要链接", style={"type": "url"}), field("重试次数", "number"),
    ],
    "运行日志表": [
        field("Agent名"), date("开始时间"), date("结束时间"), select("状态", ["成功", "失败", "跳过"]),
        field("新增条数", "number"), field("更新条数", "number"), field("错误信息", description="仅记录错误类型或代码，不记录逐字稿和密钥。"),
    ],
}

# This helper keeps the view rolling across weeks, including year boundaries.
WEEK_FIELD = field("本周到期", "formula", description="视图辅助字段；按周一到周日滚动。",
    expression='IF(AND(NOT(ISBLANK([截止日])),[截止日]>=TODAY()-WEEKDAY(TODAY(),2)+1,[截止日]<TODAY()-WEEKDAY(TODAY(),2)+8),"是","否")')

OPEN = ["状态", "disjoint", ["完成", "取消"]]
VIEWS = {
    "今天": {"logic": "and", "conditions": [["截止日", "==", "Today"], OPEN]},
    "本周": {"logic": "and", "conditions": [["本周到期", "==", "是"], OPEN]},
    "待确认": {"logic": "and", "conditions": [["状态", "intersects", ["待确认"]]]},
    "等别人的": {"logic": "and", "conditions": [["方向", "intersects", ["别人答应我的"]], OPEN]},
    "已延期": {"logic": "and", "conditions": [["延期次数", ">", 0]]},
}

SEEDS = [{"名称": name, "启用": False, "能否AI预做": False} for name in [
    "团队周期复盘", "社媒数据采集", "每日内容发布", "每周内容发布", "需求评审", "周报",
]]
