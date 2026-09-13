from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application, CallbackContext

from .config import Settings
from .models import utcnow
from .poller import FeedPoller
from .repository import FeedRepository

logger = logging.getLogger(__name__)


class FeedScheduler:
    def __init__(self, repository: FeedRepository, poller: FeedPoller, settings: Settings) -> None:
        self.repository = repository
        self.poller = poller
        self.settings = settings
        self._tick_lock = asyncio.Lock()

    async def start(self, application: Application) -> None:
        application.job_queue.run_repeating(
            self.tick,
            interval=self.settings.poll_tick_seconds,
            first=0,
            name="feed-scheduler",
        )
        logger.info("Feed scheduler started; tick=%ss", self.settings.poll_tick_seconds)

    async def tick(self, context: CallbackContext) -> None:
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            feeds = await self.repository.due_feeds(utcnow())
            if not feeds:
                return
            results = await asyncio.gather(
                *(self.poller.poll_feed(feed.id, context.bot) for feed in feeds),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    logger.exception("Unhandled feed polling error", exc_info=result)
                elif result.error:
                    logger.warning("Feed %s: %s", result.feed_id, result.error)
                else:
                    logger.info(
                        "Feed %s: discovered=%s posted=%s skipped=%s",
                        result.feed_id,
                        result.discovered,
                        result.posted,
                        result.skipped,
                    )

