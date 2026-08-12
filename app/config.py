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

    @property
    def public_url(self) -> str:
        return f"https://{self.railway_public_domain}" if self.railway_public_domain else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
