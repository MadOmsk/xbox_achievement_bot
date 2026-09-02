"""Application settings, read once from the environment (SPEC section 10)."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    bot_token: SecretStr
    # NoDecode: without it pydantic-settings reads the env value as JSON, and
    # "1,2" is not JSON — a single id would arrive as a bare int instead.
    admin_tg_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # Microsoft / Azure
    azure_client_id: str
    azure_client_secret: SecretStr
    oauth_redirect_url: str
    oauth_listen_host: str = "0.0.0.0"
    oauth_listen_port: int = 8080

    # Token encryption at rest
    fernet_key: SecretStr

    # Poller intervals, seconds (SPEC 5.2, 5.3)
    presence_interval_in_game: int = 60
    presence_interval_online: int = 120
    presence_interval_offline: int = 300
    presence_interval_idle: int = 900
    achievement_poll_interval: int = 120
    token_refresh_margin: int = 300

    backfill_concurrency: int = 2

    # Catch-up after downtime (SPEC 5.8)
    catchup_publish_window_hours: int = 24
    catchup_max_titles: int = 20

    db_path: Path = Path("data/bot.db")
    log_level: str = "INFO"
    tz: str = "Europe/Moscow"

    @field_validator("admin_tg_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: object) -> object:
        """ADMIN_TG_IDS is a comma-separated string in .env."""
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        return value

    def is_admin(self, tg_id: int) -> bool:
        return tg_id in self.admin_tg_ids


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from .env
