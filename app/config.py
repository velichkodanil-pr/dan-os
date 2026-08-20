"""DAN.OS settings loaded from environment variables."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Build identity. One constant, updated per round — /health/live and /start
# read it instead of carrying a hardcoded round number that goes stale.
APP_VERSION = "r6.1d"
APP_RELEASE = "R6.1D — order aggregates from our own data"
SCANNER_BUILD = 2   # app.core.secret_policy.SCANNER_VERSION, surfaced in /health


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    webhook_secret: str = ""
    owner_telegram_id: int = 0
    database_url: str = ""
    railway_public_domain: str = ""  # injected by Railway

    # Round 1: providers
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    extractor: str = "haiku"  # haiku | mock
    transcriber: str = "openai"  # openai | mock
    model_extract: str = "claude-haiku-4-5"
    stt_model: str = "gpt-4o-mini-transcribe"
    tz_name: str = "Europe/Kyiv"

    # Round 2: Google + rituals
    google_client_id: str = ""
    google_client_secret: str = ""
    cred_key: str = ""  # Fernet key for encrypting stored OAuth tokens
    brief_time: str = "07:30"
    checkin_time: str = "21:30"

    # Round 3a: knowledge base + digest
    embedder: str = "openai"  # openai | mock
    embed_model: str = "text-embedding-3-small"
    digest_times: str = "13:00,18:30"
    weekly_time: str = "19:00"  # Sunday coverage report

    # Full chat engine (agentic: model reaches for data via tools)
    chat_model: str = "claude-opus-5"  # set "mock" or empty to disable
    chat_effort: str = "high"  # adaptive-thinking effort (sonnet 5+)
    chat_thinking_budget: int = 1500  # budget-style thinking (opus 4.x)
    chat_history_window: int = 24
    web_search_max_uses: int = 3

    # Round 4: Mini App + TravelON pulse
    webapp_max_age: int = 3600  # initData freshness window, seconds
    travelon_token: str = ""  # full-access report token — secret, never logged

    # Round 4b: voice replies (TTS)
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "ash"
    tts_max_chars: int = 600  # longer replies stay text-only

    # TravelON owner pack: daily debt alert (empty string disables)
    travelon_sync_time: str = "04:30"  # nightly order-cache warm-up
    debt_alert_time: str = "10:00"

    # Cowork knowledge channel: enables POST /admin/ingest when set
    admin_token: str = ""

    # Full-Drive indexing cap per account per run (/drive_all)
    drive_index_max: int = 300

    # R6.1A: autonomous wiki compilation is OFF by default. Compilation is a
    # provider call over stored sources, so it stays opt-in AND gated on a
    # completed local security scan — see app/core/security.py.
    auto_wiki_compile_enabled: bool = False

    # R6.1A.1 (owner decision): passwords in business tables are searchable
    # working data for this single-owner bot — the bot is meant to answer
    # «який логін/пароль до партнера X». Only HARD technical secrets (API keys,
    # OAuth/bearer tokens, private keys, session cookies, recovery codes, seed
    # phrases) are blocked from the knowledge base. Set true to also block
    # password values (the original, stricter R6.1A behaviour).
    quarantine_passwords: bool = False

    @property
    def public_url(self) -> str:
        return f"https://{self.railway_public_domain}" if self.railway_public_domain else ""

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url.split("?", 1)[0] if "?sslmode" in url else url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
