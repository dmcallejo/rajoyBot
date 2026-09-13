from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .bot import build_application
from .config import Settings
from .database import Database
from .poller import FeedPoller
from .repository import FeedRepository
from .scheduler import FeedScheduler


def _configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    settings = Settings.from_env()
    _configure_logging()
    if settings.database_url.startswith("sqlite"):
        Path("data").mkdir(parents=True, exist_ok=True)

    database = Database(settings.database_url)
    asyncio.run(database.init())
    repository = FeedRepository(database)
    poller = FeedPoller(repository, settings)
    scheduler = FeedScheduler(repository, poller, settings)
    application = build_application(settings, database, poller, scheduler)
    try:
        application.run_polling()
    finally:
        # post_shutdown closes the HTTP client during normal PTB shutdown.
        # Dispose the SQLAlchemy engine as well; this is safe if PTB stopped early.
        asyncio.run(database.close())


if __name__ == "__main__":
    main()

