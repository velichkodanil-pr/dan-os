"""DAN.OS settings loaded from environment variables."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Build identity. One constant, updated per round — /health/live and /start
# read it instead of carrying a hardcoded round number that goes stale.
APP_VERSION = "r8.1.1"
APP_RELEASE = ("R8.1 + хотфікс R7.1 — дайджест заявок TravelON; ритуали "
               "не губляться: екранування, запасний текст, паузи")
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
    chat_model: str = "claude-opus-5-5"  # set "mock" or empty to disable
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

    # Round 7: English coach. A single daily nudge, and only when the
    # day was actually missed — a reminder that fires after a finished
    # session is how a habit tracker gets muted. Empty disables it.
    english_time: str = "20:00"

    # R8: home power status. Deye Cloud mails «grid lost / restored» per
    # station; DAN.OS turns them into ONE pinned status card (personal domain,
    # deterministic, no model). Empty POWER_MAIL_FROM disables the job.
    power_mail_from: str = "noreply@deye.cloud"
    power_poll_seconds: int = 120
    power_debounce_minutes: int = 3   # blinks shorter than this never push
    power_push: bool = True           # silent push on a settled change
    # R8 stage 2 (optional): Deye Cloud OpenAPI for battery / PV / load and
    # «inverter offline». All four must be set; the token lives in memory only.
    deye_app_id: str = ""
    deye_app_secret: str = ""
    deye_email: str = ""
    deye_password_sha256: str = ""
    deye_base_url: str = "https://eu1-developer.deyecloud.com/v1.0"
    deye_offline_minutes: int = 20

    # R8.1: TravelON booking feed — a daily digest instead of ~40 «бронювання
    # готове» e-mails, plus alerts on exceptions only. Empty time disables
    # the digest; interval 0 disables the alerts. Thresholds compare an
    # APPROXIMATE EUR value (EUR, USD); other currencies never trigger them.
    bookings_digest_time: str = "20:30"
    bookings_alert_interval_min: int = 10
    bookings_alert_hours: str = "08:00-22:00"  # Kyiv; no pushes at night
    bookings_alert_big_eur: float = 5000
    bookings_alert_group: int = 8
    bookings_alert_lastminute_days: int = 3
    bookings_alert_cancel_eur: float = 3000

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
