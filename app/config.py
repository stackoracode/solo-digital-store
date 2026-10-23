"""Application settings. All secrets come from environment variables only (see .env.example)."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["development", "test", "staging", "production"] = "development"
    app_name: str = "Solo Digital Store"
    public_base_url: str = "http://localhost:8000"
    # Hostnames served by the default (self-operated) store. Any other Host header must match a
    # verified StoreDomain row, otherwise the request is rejected (host header spoofing defence).
    primary_hosts: CsvList = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    trusted_proxy_cidrs: CsvList = Field(default_factory=list)
    trust_cf_connecting_ip: bool = False

    database_url: str = "postgresql+psycopg://store:store@localhost:5432/store"
    redis_url: str = ""
    db_pool_size: int = 10

    secret_key: str = "dev-insecure-secret-change-me"
    # Comma-separated Fernet keys; first key encrypts, all keys decrypt (rotation).
    data_encryption_keys: str = ""
    fingerprint_key: str = "dev-insecure-fingerprint-key"

    session_ttl_hours: int = 24 * 14
    admin_session_idle_minutes: int = 30
    admin_require_mfa: bool = True
    cookie_secure: bool = False

    max_body_bytes: int = 1_000_000
    max_upload_bytes: int = 5_000_000

    # Login providers (each is enabled only when its credentials are set) ----------------
    email_code_ttl_minutes: int = 10
    email_code_max_attempts: int = 5
    google_client_id: str = ""
    google_client_secret: str = ""
    telegram_bot_username: str = ""
    telegram_bot_token: str = ""
    telegram_auth_max_age_seconds: int = 600
    # Generic OIDC provider slot, e.g. a self-hosted Logto tenant (ADR-007).
    oidc_name: str = ""
    oidc_display_name: str = "SSO"
    oidc_metadata_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""

    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "Solo Digital Store <no-reply@example.com>"
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    email_max_per_recipient_hour: int = 10

    # Payments ---------------------------------------------------------------
    payment_attempt_ttl_minutes: int = 30
    order_unpaid_ttl_minutes: int = 120
    mock_payments_enabled: bool = True
    mock_webhook_secret: str = "dev-mock-webhook-secret"

    waffo_api_base: str = "https://api.waffo.ai"
    waffo_merchant_id: str = ""
    waffo_private_key: str = ""
    waffo_webhook_public_key_test: str = ""
    waffo_webhook_public_key_prod: str = ""
    # Hard gate: live Waffo payments additionally require per-product merchant approval flags.
    waffo_live_enabled: bool = False

    usdt_xpub: str = ""
    usdt_live_enabled: bool = False
    usdt_rate_source: Literal["peg", "coingecko"] = "peg"
    usdt_networks_file: str = "config/usdt_networks.json"

    # Storage ----------------------------------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    local_media_dir: str = "var/media"
    s3_endpoint_url: str = ""
    s3_bucket_public: str = ""
    s3_public_base_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    ai_endpoint_allowlist: CsvList = Field(
        default_factory=lambda: [
            "api.openai.com",
            "generativelanguage.googleapis.com",
            "api.anthropic.com",
        ]
    )

    worker_poll_seconds: float = 1.0
    log_level: str = "INFO"

    @field_validator("primary_hosts", "trusted_proxy_cidrs", "ai_endpoint_allowlist", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def _valid_cidrs(cls, v: list[str]) -> list[str]:
        for cidr in v:
            ipaddress.ip_network(cidr, strict=False)
        return v

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.env == "production":
            problems = []
            if "insecure" in self.secret_key or len(self.secret_key) < 32:
                problems.append("SECRET_KEY must be a random value of >= 32 chars")
            if "insecure" in self.fingerprint_key or len(self.fingerprint_key) < 32:
                problems.append("FINGERPRINT_KEY must be a random value of >= 32 chars")
            if not self.data_encryption_keys:
                problems.append("DATA_ENCRYPTION_KEYS is required")
            if self.mock_payments_enabled:
                problems.append("MOCK_PAYMENTS_ENABLED must be false in production")
            if not self.cookie_secure:
                problems.append("COOKIE_SECURE must be true in production")
            if not self.turnstile_secret_key:
                problems.append("TURNSTILE_SECRET_KEY is required in production")
            if not self.public_base_url.startswith("https://"):
                problems.append("PUBLIC_BASE_URL must be https in production")
            if problems:
                raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def mock_allowed(self) -> bool:
        return self.mock_payments_enabled and not self.is_production


@lru_cache
def get_settings() -> Settings:
    return Settings()
