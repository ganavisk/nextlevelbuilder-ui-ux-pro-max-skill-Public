"""Application configuration.

Every runtime setting is env-driven (12-factor). `.env` is read in local
development; in staging/production values come from the platform secret store.
Settings are validated at startup — a missing required secret fails fast
rather than surfacing mid-call.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_SECRETS = {"dev-only-secret", "change-me-generate-with-openssl-rand-hex-32"}


# Where local secrets are read from, resolved from this file rather than the
# working directory: `make dev` runs inside apps/api, so a cwd-relative ".env"
# silently ignored the repo-root file the docs (and docker compose) use.
# Both are read; the repo-root file wins when both set a value. In the
# production image neither exists and values come from the environment.
_API_DIR = Path(__file__).resolve().parents[3]
ENV_FILES = (_API_DIR / ".env", _API_DIR.parent.parent / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILES, env_file_encoding="utf-8", extra="ignore")

    # Application
    app_env: Literal["development", "staging", "production", "test"] = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    secret_key: str = Field(default="dev-only-secret", description="JWT / signing key")
    cors_origins: str = "http://localhost:3000"
    # Publicly reachable base URL of the Next.js dashboard. Used only to build
    # the browser redirect at the end of an OAuth connect flow (the API
    # redirects the browser back to {dashboard}/settings?connected=... after
    # exchanging the provider's authorization code) — the dashboard never
    # calls this URL itself.
    dashboard_base_url: str = "http://localhost:3000"
    # JWT lifetimes. Short-lived on purpose: no refresh-token/session-revocation
    # mechanism exists yet (Future improvement, PHASE-7 doc) — a token that
    # leaks is only useful until it expires.
    access_token_ttl_minutes: int = 60
    # Login throttling (core/ratelimit.py). Counted per attempt, refunded on
    # success, so these are effectively "failed attempts per window".
    login_window_seconds: int = 900
    login_max_attempts_per_account: int = 10
    login_max_attempts_per_ip: int = 60
    oauth_state_ttl_minutes: int = 10

    # Data stores
    database_url: str = "postgresql+asyncpg://receptionist:receptionist@localhost:5432/receptionist"
    redis_url: str = "redis://localhost:6379/0"
    credentials_encryption_key: str = ""

    # Voice layer
    voice_provider: Literal["vapi", "retell"] = "vapi"
    vapi_api_key: str = ""
    vapi_webhook_secret: str = ""
    # Publicly reachable base URL of THIS api, e.g. https://api.example.com or
    # an https tunnel in development. Written into the vendor-side agent as its
    # server URL, so the vendor knows where to send call + tool-call webhooks.
    # Without it, provisioning has nowhere to point and the agent can't call
    # any tool.
    public_webhook_base_url: str = ""

    # Telephony / SMS
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_messaging_service_sid: str = ""

    # LLM
    anthropic_api_key: str = ""
    # The SDK defaults (10-minute read timeout, 2 retries) are meant for batch
    # work; a caller is on the phone. These bound a single upstream attempt;
    # `tool_deadline_seconds` below bounds the whole tool call.
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    llm_model_primary: str = "claude-sonnet-5"
    llm_model_fast: str = "claude-haiku-4-5"

    # Embeddings
    voyage_api_key: str = ""
    embedding_timeout_seconds: float = 15.0
    embedding_max_retries: int = 1
    embedding_model: str = "voyage-3.5"
    embedding_dimensions: int = 1024

    # Integrations
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    hubspot_client_id: str = ""
    hubspot_client_secret: str = ""
    resend_api_key: str = ""
    email_from: str = "notifications@example.com"

    # Observability
    sentry_dsn: str = ""  # empty = error reporting disabled

    # Hard ceiling on a read-only in-call tool (FAQ, availability), covering
    # every upstream hop and retry inside it. Past this the caller gets the
    # tool's spoken fallback instead of silence. Not applied to booking: see
    # ToolExecutor.
    tool_deadline_seconds: float = 8.0

    # Error reporting: an empty `sentry_dsn` (above) disables it; see
    # core/observability.py.
    sentry_traces_sample_rate: float = 0.0
    app_release: str = ""  # git SHA; falls back to the platform's commit variable

    # Guardrails
    max_call_duration_seconds: int = 900
    tenant_daily_spend_cap_usd: float = 50.0

    @field_validator("database_url")
    @classmethod
    def _use_the_async_driver(cls, value: str) -> str:
        """Platforms (Railway, Heroku, Neon...) hand out `postgres://` or
        `postgresql://` URLs. The app needs the asyncpg driver spelled out; a
        bare scheme otherwise fails at first connect with a confusing driver
        error instead of at config time."""
        for bare in ("postgres://", "postgresql://"):
            if value.startswith(bare):
                return "postgresql+asyncpg://" + value[len(bare) :]
        return value

    @model_validator(mode="after")
    def _require_real_secret_outside_dev(self) -> "Settings":
        # secret_key signs dashboard login tokens. The development default is
        # public (it is in this repo), so a staging/production process that
        # started with it would accept forged tokens for any tenant.
        if self.app_env in ("staging", "production") and (
            self.secret_key in _PLACEHOLDER_SECRETS or len(self.secret_key) < 32
        ):
            raise ValueError(
                "SECRET_KEY must be set to a random value of at least 32 characters "
                "in staging/production (generate one with: openssl rand -hex 32)"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
