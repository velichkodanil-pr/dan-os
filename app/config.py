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
