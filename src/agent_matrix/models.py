from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    lark_brand: Literal["feishu", "lark"] = "feishu"
    # Items stay on this computer (item_store: device / notion) but Feishu/Lark is still read like Slack:
    # meetings, chats and tasks come in; nothing is written to a Feishu table.
    feishu_as_source: bool = False   # "lark": the international Lark (larksuite.com); same as lark-cli's --brand
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
    chat_skip_chats: list[str] = Field(default_factory=list)  # chats whose name contains any of these are not read
    chat_skip_words: list[str] = Field(default_factory=list)  # messages containing any of these are skipped
    # Chat todos: @you or urgent lines are extracted at once; the rest whenever there are new lines and this many
    # minutes passed since the last round (0 = only at chat_review_times). Outside from/until only urgent ones run.
    chat_review_every_minutes: int = Field(default=5, ge=0, le=240)
    chat_review_times: list[str] = Field(default_factory=lambda: ["11:30", "14:30", "17:30", "20:30"])
    chat_extract_from: str = "08:30"
    chat_extract_until: str = "22:00"
    feishu_tasks_enabled: bool = True
    # Slack as a second chat source: a user token (xoxp-); empty = env SLACK_USER_TOKEN; neither = off.
    slack_token: str = ""
    # WeChat as a chat source, read on this computer through the wechat-cli command (the owner's own local messages).
    wechat_enabled: bool = False
    wechat_binary: str = ""          # path to wechat-cli; empty = find it on PATH
    # Words too common in meeting titles to link items by topic, e.g. the company name.
    topic_stopwords: list[str] = Field(default_factory=list)
    # The AI employee (worker.py): up to ten concurrent jobs, one GPT conversation per item.
    # Where messages to the owner go: "local" = the local client's inbox (Feishu is only the database),
    # "feishu" = Feishu private chat as before.
    notify_channel: Literal["local", "feishu"] = "local"
    # The 08:30 / 21:00 briefs: "feishu" = also sent to the owner on Feishu, "local" = only in the 心响 inbox.
    brief_channel: Literal["feishu", "local"] = "feishu"
    # What happens to things other people send the owner (chats, meetings, Feishu tasks):
    #   "all"      全能: anything asked of the owner becomes an item and the AI starts on it
    #   "accepted" 全自动: only what the owner agreed to take ("好的", "我来") becomes an item, then the AI starts
    #   "manual"   手动: nothing is picked up; the owner adds items by hand
    intake_mode: Literal["all", "accepted", "manual"] = "all"
    # Where the items live: "local" = state/items.db on this computer (instant clicks), with the Feishu
    # table kept as a backup written in the background; "feishu" = read and write the Feishu table directly;
    # "notion" = state/items.db, backed up to a Notion database, no Feishu needed; "device" = state/items.db only.
    item_store: Literal["local", "feishu", "notion", "device"] = "feishu"
    notion_token: str = ""        # Notion internal integration secret (ntn_… / secret_…)
    notion_database_id: str = ""  # the 心响事项 database created under the page the owner shared
    app_port: int = Field(default=47865, ge=1024, le=65535)  # 8765 is a common dev port; keep 心响 out of its way
    worker_enabled: bool = True
    worker_codex: str = ""          # path to the codex command; empty = find it (PATH, then the Cindy bundle)
    worker_model: str = "gpt-6-astra"
    worker_effort: str = "high"     # doing the work
    extract_effort: str = "medium"  # picking todos out of meetings and chats
    worker_timeout_minutes: int = Field(default=30, ge=5, le=120)
    worker_claude: str = ""          # path to the claude command; empty = find it on PATH (used when 系统 → 本机 AI is Claude)
    worker_claude_model: str = ""    # e.g. opus / sonnet; empty = the claude account's default
    worker_gemini: str = ""          # path to the gemini command; empty = find it on PATH (本机 Gemini)
    worker_gemini_model: str = ""    # e.g. gemini-2.5-pro; empty = the Gemini CLI's default
    worker_gemini_api_key: str = ""  # optional Gemini API key (GEMINI_API_KEY); empty = however the gemini command is signed in
    # 记忆增强 (optional): Tencent Hunyuan Hy Memory with the owner's own API. Kept in config.yaml on this computer only.
    memory_boost_enabled: bool = False
    memory_base_url: str = ""            # OpenAI-compatible endpoint for the language model
    memory_api_key: str = ""
    memory_llm_model: str = ""
    memory_embed_base_url: str = ""      # empty = the same endpoint and key as above
    memory_embed_api_key: str = ""
    memory_embed_model: str = ""
    memory_embed_dims: int = Field(default=1024, ge=64, le=8192)
    worker_gemini_sandbox: bool = True  # macOS: run it in the system sandbox, so it only writes inside the job's folder
    # MCP tools the local Claude may use while working, e.g. ["mcp__claude_ai_Notion__notion-fetch"]. Empty = none
    # (safe mode). Non-empty: the owner's own hooks/plugins stay off, only these MCP tools are allowed.
    worker_mcp_tools: list[str] = Field(default_factory=list)
    worker_claudeai_mcp: bool = False  # also the claude.ai connectors (they shadow same-server plugins until signed in)
    # exact MCP tool names let through the send/create ban (e.g. Slack's send, for cw5-pr-notify); asked in 开发模式
    worker_mcp_send: list[str] = Field(default_factory=list)
    worker_concurrency: int = Field(default=10, ge=1, le=20)  # jobs the built-in GPT works on at the same time

    @model_validator(mode="after")
    def _feishu_free_items_table(self):
        # ponytail: without Feishu the items table is a name only; every call on it is routed to state/items.db.
        if self.item_store in ("notion", "device"):
            self.tables.setdefault("事项表", "local")
        return self


class MatrixError(RuntimeError):
    """User-facing error; never include raw CLI output or credentials."""


class MatrixBusy(MatrixError):
    """Another local operation holds the project lock; retry on next scheduled tick."""
