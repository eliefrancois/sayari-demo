"""Runtime configuration. All env-driven, validated on startup."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    neo4j_uri: str = Field(default="", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: str = Field(default="", alias="NEO4J_PASSWORD")

    upstash_redis_rest_url: str = Field(default="", alias="UPSTASH_REDIS_REST_URL")
    upstash_redis_rest_token: str = Field(default="", alias="UPSTASH_REDIS_REST_TOKEN")

    # L2 episodic memory (doc 09 Phase D): a SEPARATE Upstash Vector index (not
    # the Redis above). Holds one structured episode per turn for fuzzy semantic
    # recall of OLD turns via the recall_memory tool. Everything is a graceful
    # NO-OP unless BOTH the index creds are present AND the flag is on, so the
    # live demo is unaffected until the user provisions it and flips the flag.
    upstash_vector_rest_url: str = Field(default="", alias="UPSTASH_VECTOR_REST_URL")
    upstash_vector_rest_token: str = Field(default="", alias="UPSTASH_VECTOR_REST_TOKEN")
    episodic_memory_enabled: bool = Field(default=False, alias="EPISODIC_MEMORY_ENABLED")

    opensanctions_api_url: str = Field(
        default="https://api.opensanctions.org", alias="OPENSANCTIONS_API_URL"
    )
    opensanctions_api_key: str = Field(default="", alias="OPENSANCTIONS_API_KEY")

    # Sayari Graph API. The SDK handles token rotation + 429 retry internally.
    sayari_client_id: str = Field(default="", alias="SAYARI_CLIENT_ID")
    sayari_client_secret: str = Field(default="", alias="SAYARI_CLIENT_SECRET")

    allowed_origins: str = Field(
        default="http://localhost:3000", alias="ALLOWED_ORIGINS"
    )

    agent_impl: Literal["native", "graph"] = Field(default="native", alias="AGENT_IMPL")

    # Token-level streaming of the agent's text (reasoning narration + the final
    # answer/summary narrative) over SSE. Only the LangGraph impl streams; the
    # flag is a safety valve to fall back to whole-response emits if needed.
    stream_tokens: bool = Field(default=True, alias="STREAM_TOKENS")

    # Lightweight intent router: a cheap structured classification call BEFORE the
    # main loop that labels the turn's intent, narrows the tool subset, and injects
    # targeted guidance. The flag is a safety valve — off => full toolset, no
    # router call. The model is a small/fast one so the added latency/credits are
    # minimal relative to the Sonnet investigation loop.
    intent_router_enabled: bool = Field(default=True, alias="INTENT_ROUTER_ENABLED")
    intent_router_model: str = Field(
        default="claude-haiku-4-5-20251001", alias="INTENT_ROUTER_MODEL"
    )

    # LangSmith tracing (only used by the LangGraph impl). When tracing is on and
    # an API key is present, LangChain/LangGraph auto-trace every graph run, node,
    # and LLM call — no manual span wiring needed for the graph path.
    langchain_tracing_v2: bool = Field(default=False, alias="LANGCHAIN_TRACING_V2")
    langchain_api_key: str = Field(default="", alias="LANGCHAIN_API_KEY")
    langchain_project: str = Field(
        default="entity-risk-resolver", alias="LANGCHAIN_PROJECT"
    )
    langchain_endpoint: str = Field(
        default="https://api.smith.langchain.com", alias="LANGCHAIN_ENDPOINT"
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    port: int = Field(default=8080, alias="PORT")

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


def apply_langsmith_env(settings: "Settings") -> bool:
    """Propagate LangSmith settings into os.environ.

    LangChain reads LANGCHAIN_* from the process environment, but pydantic-
    settings only loads them into the Settings object (especially when they come
    from a .env file). Mirror them back so tracing turns on. Returns whether
    tracing is active."""
    if settings.langchain_tracing_v2 and settings.langchain_api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key
        os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project
        os.environ["LANGCHAIN_ENDPOINT"] = settings.langchain_endpoint
        return True
    return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
