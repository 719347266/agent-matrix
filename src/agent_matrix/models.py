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
    # Words too common in meeting titles to link items by topic, e.g. the company name.
    topic_stopwords: list[str] = Field(default_factory=list)


class MatrixError(RuntimeError):
    """User-facing error; never include raw CLI output or credentials."""


class MatrixBusy(MatrixError):
    """Another local operation holds the project lock; retry on next scheduled tick."""
