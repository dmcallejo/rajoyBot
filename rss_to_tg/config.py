from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    database_url: str
    default_interval_minutes: int = 30
    min_interval_minutes: int = 5
    max_interval_minutes: int = 24 * 60
    poll_tick_seconds: int = 30
    max_articles_per_poll: int = 10
    post_existing_on_first_run: bool = False
    request_timeout_seconds: int = 20
    user_agent: str = "rss-to-tg/0.1 (+https://github.com/example/rss-to-tg)"

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "Settings":
        """Load settings from the environment, optionally loading a local .env first."""
        load_dotenv(dotenv_path=env_file)

        token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ValueError("BOT_TOKEN is required")

        default_database = "sqlite+aiosqlite:///./data/rss-to-tg.sqlite3"
        settings = cls(
            bot_token=token.strip(),
            database_url=os.getenv("DATABASE_URL", default_database).strip(),
            default_interval_minutes=_int_env("DEFAULT_INTERVAL_MINUTES", 30),
            min_interval_minutes=_int_env("MIN_INTERVAL_MINUTES", 5),
            max_interval_minutes=_int_env("MAX_INTERVAL_MINUTES", 24 * 60),
            poll_tick_seconds=_int_env("POLL_TICK_SECONDS", 30),
            max_articles_per_poll=_int_env("MAX_ARTICLES_PER_POLL", 10),
            post_existing_on_first_run=_bool_env("POST_EXISTING_ON_FIRST_RUN", False),
            request_timeout_seconds=_int_env("REQUEST_TIMEOUT_SECONDS", 20),
            user_agent=os.getenv(
                "RSS_USER_AGENT",
                "rss-to-tg/0.1 (+https://github.com/example/rss-to-tg)",
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.min_interval_minutes < 1:
            raise ValueError("MIN_INTERVAL_MINUTES must be at least 1")
        if not self.min_interval_minutes <= self.default_interval_minutes <= self.max_interval_minutes:
            raise ValueError("DEFAULT_INTERVAL_MINUTES must be between the configured interval limits")
        if self.max_interval_minutes < self.min_interval_minutes:
            raise ValueError("MAX_INTERVAL_MINUTES must be greater than or equal to MIN_INTERVAL_MINUTES")
        if self.poll_tick_seconds < 5:
            raise ValueError("POLL_TICK_SECONDS must be at least 5")
        if self.max_articles_per_poll < 1:
            raise ValueError("MAX_ARTICLES_PER_POLL must be at least 1")
        if self.request_timeout_seconds < 1:
            raise ValueError("REQUEST_TIMEOUT_SECONDS must be at least 1")
