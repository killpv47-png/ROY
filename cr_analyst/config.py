"""Centralised, validated configuration (environment / .env)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- required in production -------------------------------------------------
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    database_url: str = Field(default="sqlite+aiosqlite:///./data/dev.db", alias="DATABASE_URL")
    app_encryption_key: str = Field(default="", alias="APP_ENCRYPTION_KEY")

    # --- optional ---------------------------------------------------------------
    admin_id: int | None = Field(default=None, alias="ADMIN_ID")
    gh_pat: str = Field(default="", alias="GH_PAT")
    github_repository: str = Field(default="", alias="GITHUB_REPOSITORY")  # owner/repo
    github_ref_name: str = Field(default="main", alias="GITHUB_REF_NAME")
    github_run_id: str = Field(default="", alias="GITHUB_RUN_ID")
    runner_workflow_file: str = Field(default="bot-runner.yml", alias="RUNNER_WORKFLOW_FILE")

    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_model: str = Field(default="gpt-4o-mini", alias="LLM_MODEL")
    llm_base_url: str = Field(default="https://api.openai.com/v1", alias="LLM_BASE_URL")
    llm_temperature: float = Field(default=0.3, alias="LLM_TEMPERATURE")
    llm_max_tokens: int = Field(default=900, alias="LLM_MAX_TOKENS")
    llm_max_tool_rounds: int = Field(default=5, alias="LLM_MAX_TOOL_ROUNDS")

    clash_api_token: str = Field(default="", alias="CLASH_API_TOKEN")
    clash_api_base: str = Field(default="https://api.clashroyale.com/v1", alias="CLASH_API_BASE")
    expected_egress_ip: str = Field(default="", alias="EXPECTED_EGRESS_IP")

    # Web search: "auto" = Tavily when a Tavily key is present, otherwise DuckDuckGo HTML.
    search_api_provider: str = Field(default="auto", alias="SEARCH_API_PROVIDER")  # auto|tavily|brave|duckduckgo
    search_api_key: str = Field(default="", alias="SEARCH_API_KEY")  # Brave key (optional)
    tavily_api_key: str = Field(default="", validation_alias=AliasChoices("TAVILY_KEY", "TAVILY_API_KEY", "tavily_key"))
    tavily_search_depth: str = Field(default="basic", alias="TAVILY_SEARCH_DEPTH")  # basic|advanced|fast|ultra-fast

    # Interrupted-task resume: wait this long, then continue from the same step.
    resume_delay_seconds: int = Field(default=10, alias="RESUME_DELAY_SECONDS")
    max_resume_attempts: int = Field(default=5, alias="MAX_RESUME_ATTEMPTS")

    # --- runtime ----------------------------------------------------------------
    max_runtime_minutes: int = Field(default=330, alias="MAX_RUNTIME_MINUTES")
    shutdown_grace_seconds: int = Field(default=90, alias="SHUTDOWN_GRACE_SECONDS")
    checkpoint_interval_seconds: int = Field(default=300, alias="CHECKPOINT_INTERVAL_SECONDS")
    lease_ttl_seconds: int = Field(default=120, alias="LEASE_TTL_SECONDS")
    max_auto_restarts: int = Field(default=120, alias="MAX_AUTO_RESTARTS")
    auto_restart: bool = Field(default=True, alias="AUTO_RESTART")
    task_workers: int = Field(default=3, alias="TASK_WORKERS")

    workspace_dir: Path = Field(default=Path("./data/workspace"), alias="WORKSPACE_DIR")
    cache_dir: Path = Field(default=Path("./data/cache"), alias="CACHE_DIR")
    skills_dir: Path = Field(default=Path("./skills"), alias="SKILLS_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Deck engine
    evolution_slots: int = Field(default=2, alias="EVOLUTION_SLOTS")
    deck_weights_file: str = Field(default="", alias="DECK_WEIGHTS_FILE")

    # Retention (days)
    battle_retention_days: int = Field(default=365, alias="BATTLE_RETENTION_DAYS")
    snapshot_retention_days: int = Field(default=365, alias="SNAPSHOT_RETENTION_DAYS")
    task_retention_days: int = Field(default=60, alias="TASK_RETENTION_DAYS")

    @field_validator("admin_id", mode="before")
    @classmethod
    def _empty_admin(cls, v):  # noqa: N805
        if v in ("", None):
            return None
        return int(v)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def effective_search_provider(self) -> str:
        p = (self.search_api_provider or "auto").lower()
        if p == "auto":
            return "tavily" if self.tavily_api_key else ("brave" if self.search_api_key else "duckduckgo")
        return p

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    def secret_values(self) -> list[str]:
        """Values that must never appear in any output."""
        vals = [self.bot_token, self.gh_pat, self.app_encryption_key, self.llm_api_key,
                self.clash_api_token, self.search_api_key, self.tavily_api_key]
        if "@" in self.database_url and "://" in self.database_url:
            creds = self.database_url.split("://", 1)[1].split("@", 1)[0]
            if ":" in creds:
                vals.append(creds.split(":", 1)[1])
        return [v for v in vals if v and len(v) >= 6]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
