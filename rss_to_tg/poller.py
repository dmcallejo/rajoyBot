from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx
from telegram import Bot

from .config import Settings
from .feed_parser import FeedEntry, parse_feed
from .formatter import format_article
from .repository import FeedRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PollResult:
    feed_id: int
    discovered: int = 0
    posted: int = 0
    skipped: int = 0
    error: str | None = None


class FeedPoller:
    def __init__(self, repository: FeedRepository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
        )
        self._semaphore = asyncio.Semaphore(5)

    async def close(self) -> None:
        await self.client.aclose()

    async def poll_feed(self, feed_id: int, bot: Bot, *, force: bool = False) -> PollResult:
        async with self._semaphore:
            return await self._poll_feed(feed_id, bot, force=force)

    async def _poll_feed(self, feed_id: int, bot: Bot, *, force: bool) -> PollResult:
        feed = await self.repository.get_source(feed_id)
        if feed is None:
            return PollResult(feed_id=feed_id)
        configurations = await self.repository.active_configurations(feed_id)
        if not configurations:
            return PollResult(feed_id=feed_id)

        first_run = feed.last_success_at is None
        await self.repository.mark_checked(feed_id)
        headers: dict[str, str] = {}
        if feed.etag:
            headers["If-None-Match"] = feed.etag
        if feed.last_modified:
            headers["If-Modified-Since"] = feed.last_modified

        try:
            response = await self.client.get(feed.url, headers=headers)
            not_modified = response.status_code == 304
            if not_modified:
                entries = []
            else:
                response.raise_for_status()
                entries = parse_feed(response.content, feed.url)
                entries = sorted(
                    entries,
                    key=lambda item: item.published_at or datetime.min,
                )[-self.settings.max_articles_per_poll :]
            discovered = 0
            posted = 0
            skipped = 0
            failures: list[str] = []
            configurations = await self.repository.active_configurations(feed_id)

            for entry in entries:
                article, is_new = await self.repository.get_or_create_article(
                    feed_id=feed_id,
                    guid=entry.guid,
                    link=entry.link,
                    title=entry.title,
                    summary=entry.summary,
                    published_at=entry.published_at,
                    skip=False,
                )
                if is_new:
                    discovered += 1
                for configuration in configurations:
                    # A newly added route starts with articles discovered after it
                    # was created; old source history remains private to its route.
                    if article.seen_at < configuration.feed_started_at:
                        continue
                    delivery, is_new_delivery = await self.repository.get_or_create_delivery(
                        configuration_id=configuration.id,
                        article_id=article.id,
                        skipped=first_run and not self.settings.post_existing_on_first_run,
                    )
                    if is_new_delivery and delivery.skipped:
                        skipped += 1

            pending = await self.repository.pending_deliveries(feed_id)
            for delivery, article, configuration in pending:
                try:
                    entry = FeedEntry(
                        guid=article.guid,
                        link=article.link,
                        title=article.title,
                        summary=article.summary,
                        published_at=article.published_at,
                    )
                    text, link_preview_options = format_article(entry)
                    message = await bot.send_message(
                        chat_id=configuration.channel_id,
                        text=text,
                        parse_mode="HTML",
                        link_preview_options=link_preview_options,
                    )
                    await self.repository.mark_delivery_posted(delivery.id, message.message_id)
                    posted += 1
                except Exception as exc:  # Telegram errors vary by API response.
                    logger.exception(
                        "Could not post article %s for configuration %s",
                        article.link,
                        configuration.id,
                    )
                    await self.repository.mark_delivery_error(delivery.id, str(exc))
                    failures.append(f"{article.title}: {exc}")

            error = "; ".join(failures) if failures else None
            await self.repository.mark_success(
                feed_id,
                etag=feed.etag if not_modified else response.headers.get("etag"),
                last_modified=feed.last_modified if not_modified else response.headers.get("last-modified"),
                error=None,
            )
            return PollResult(
                feed_id=feed_id,
                discovered=discovered,
                posted=posted,
                skipped=skipped,
                error=error,
            )
        except Exception as exc:
            logger.exception("Could not poll feed %s", feed_id)
            await self.repository.mark_error(feed_id, str(exc))
            return PollResult(feed_id=feed_id, error=str(exc))
