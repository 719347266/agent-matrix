from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    stage: Literal[0] = 0
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    base_name: str = "会议与待办中心"
    owner_open_id: str = ""
    owner_name: str = ""
    owner_aliases: list[str] = Field(default_factory=list)  # how colleagues call the owner, e.g. 小王
    app_id: str = ""
    base_token: str = ""
    base_url: str = ""
    tables: dict[str, str] = Field(default_factory=dict)
    lark_binary: str = "lark-cli"
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    llm_backend: Literal["disabled", "cindy_current"] = "disabled"
    transcripts_to_llm_allowed: bool = False
    scheduling_enabled: bool = False
    meeting_watch_enabled: bool = False
    meeting_watch_started_at: str = ""
    work_start: str | None = None
    work_end: str | None = None
    work_breaks: list[list[str]] = Field(default_factory=list)
    prework_enabled: bool = False
    prework_lead_days: int = Field(default=2, ge=1, le=2)
    # Real-time sources (see listen.py): chats are read as the owner and pre-filtered by code.
    chat_watch_enabled: bool = True
    chat_review_times: list[str] = Field(default_factory=lambda: ["11:30", "14:30", "17:30", "20:30"])
    feishu_tasks_enabled: bool = True
    # Slack as a second chat source: a user token (xoxp-); empty = env SLACK_USER_TOKEN; neither = off.
    slack_token: str = ""
    # Words too common in meeting titles to link items by topic, e.g. the company name.
    topic_stopwords: list[str] = Field(default_factory=list)
    # The AI employee (worker.py): up to ten concurrent jobs, one GPT conversation per item.
    # Where messages to the owner go: "local" = the local client's inbox (Feishu is only the database),
    # "feishu" = Feishu private chat as before.
    notify_channel: Literal["local", "feishu"] = "local"
    # Where the items live: "local" = state/items.db on this computer (instant clicks), with the Feishu
    # table kept as a backup written in the background; "feishu" = read and write the Feishu table directly.
    item_store: Literal["local", "feishu"] = "feishu"
    app_port: int = Field(default=47865, ge=1024, le=65535)  # 8765 is a common dev port; keep 心响 out of its way
    worker_enabled: bool = True
    worker_codex: str = ""          # path to the codex command; empty = find it (PATH, then the Cindy bundle)
    worker_model: str = "gpt-6-astra"
    worker_effort: str = "high"     # doing the work
    extract_effort: str = "medium"  # picking todos out of meetings and chats
    worker_timeout_minutes: int = Field(default=30, ge=5, le=120)
    worker_claude: str = ""          # path to the claude command; empty = find it on PATH (used when 系统 → 本机 AI is Claude)
    worker_claude_model: str = ""    # e.g. opus / sonnet; empty = the claude account's default
    worker_concurrency: int = Field(default=10, ge=1, le=20)  # jobs the built-in GPT works on at the same time


class MatrixError(RuntimeError):
    """User-facing error; never include raw CLI output or credentials."""


class MatrixBusy(MatrixError):
    """Another local operation holds the project lock; retry on next scheduled tick."""
