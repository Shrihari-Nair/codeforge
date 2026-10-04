from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AEOS_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    database_url: str = "postgresql://aeos:aeos@localhost:5432/aeos"
    demo_schema: str = (
        "demo"  # Postgres schema holding the telemetry the observability/database tools expose
    )
    readonly_database_url: str = "postgresql://aeos_readonly:aeos_readonly@localhost:5432/aeos"
    redis_url: str = "redis://localhost:6380/0"
    otel_exporter_endpoint: str = "http://localhost:4317"
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    llm_provider: Literal["gemini", "fake"] = "fake"
    gemini_model: str = "gemini-2.5-flash"
    gemini_strong_model: str = "gemini-3.1-pro-preview"
    gemini_thinking_budget: int = 1024
    git_remote_path: str = "demo_target/build/remote/checkout-service.git"
    web_allowed_hosts: str = "localhost,127.0.0.1"
    web_corpus_dir: str = "demo_target/web_corpus"
    web_corpus_url: str = "http://127.0.0.1:8765"
    # Specialists: in-process (fast tests) or real A2A services reached over the network.
    a2a_mode: Literal["inprocess", "a2a"] = "inprocess"
    a2a_endpoints: str = (
        ""  # JSON: {"analytics": "http://h:9101,http://h2:9101", ...} (failover order)
    )
    a2a_api_key: SecretStr | None = None  # bearer token the specialist services require
    a2a_timeout_s: float = 900.0  # whole-delegation deadline
    a2a_stall_s: float = 120.0  # no event from the service for this long => treat it as dead
    # Optional real-GitHub publishing: on approval, `create_pull_request` also pushes the aeos/*
    # branch to GitHub and opens a real PR there. Off unless explicitly enabled.
    github_publish: bool = False
    github_repo: str | None = None  # "owner/name" (a github.com URL is accepted too)
    github_token: SecretStr | None = None
    # Branch on GitHub that PRs target. "@default" = the repository's own default branch (real
    # target repo); anything else is a dedicated branch holding the demo code.
    github_base_branch: str = "aeos-demo"
    target_enabled: bool = True  # allow "change" tasks on the real repo (needs repo + token)
    target_mirror_dir: str = "demo_target/build/target"  # local bare mirror of the real repo
    github_api_url: str = "https://api.github.com"
    github_git_url: str | None = None  # default https://github.com/<repo>.git (tests override)
    memory_enabled: bool = True
    memory_token_budget: int = 1200  # max tokens of recalled memory injected into planning
    cors_origins: str = "http://localhost:3100,http://127.0.0.1:3100"  # dashboard origins
    langfuse_public_url: str = "http://localhost:3000"  # for deep links from the dashboard
    telemetry_enabled: bool = True  # export traces/metrics to the OTLP collector
    approval_ttl_s: float = 86_400.0  # pending approvals expire after this long
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @property
    def target_ready(self) -> bool:
        """Can "change" tasks run? (a real repo and a token with access to it are configured)"""
        return bool(self.target_enabled and self.github_repo and self.github_token)

    @field_validator("demo_schema")
    @classmethod
    def _check_schema(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_]{0,60}", v):
            raise ValueError("demo_schema must be a lowercase identifier")
        return v

    @field_validator("database_url", "readonly_database_url")
    @classmethod
    def _check_database_url(cls, v: str) -> str:
        if not v.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("database URLs must start with postgresql://")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
