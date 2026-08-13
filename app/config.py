"""DAN.OS settings loaded from environment variables."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Full chat engine (real assistant-grade replies)
    chat_model: str = "claude-sonnet-5"  # set "mock" or empty to disable
    chat_effort: str = "high"  # adaptive-thinking effort: low|medium|high|max
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
    debt_alert_time: str = "10:00"

    # Cowork knowledge channel: enables POST /admin/ingest when set
    admin_token: str = ""

    # Full-Drive indexing cap per account per run (/drive_all)
    drive_index_max: int = 300

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
