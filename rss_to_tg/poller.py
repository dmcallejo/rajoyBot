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
        feed = await self.repository.get_feed(feed_id)
        if feed is None or (not feed.enabled and not force):
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
            if response.status_code == 304:
                await self.repository.mark_success(
                    feed_id,
                    etag=feed.etag,
                    last_modified=feed.last_modified,
                )
                return PollResult(feed_id=feed_id)
            response.raise_for_status()
            entries = parse_feed(response.content, feed.url)
            if not entries:
                await self.repository.mark_success(
                    feed_id,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                )
                return PollResult(feed_id=feed_id)

            entries = sorted(
                entries,
                key=lambda item: item.published_at or datetime.min,
            )[-self.settings.max_articles_per_poll :]
            discovered = 0
            posted = 0
            skipped = 0
            failures: list[str] = []

            for entry in entries:
                article, is_new = await self.repository.get_or_create_article(
                    feed_id=feed_id,
                    guid=entry.guid,
                    link=entry.link,
                    title=entry.title,
                    summary=entry.summary,
                    published_at=entry.published_at,
                    skip=first_run and not self.settings.post_existing_on_first_run,
                )
                if is_new:
                    discovered += 1
                if article.posted_at is not None or article.skipped:
                    skipped += 1
                    continue

                try:
                    text, link_preview_options = format_article(entry)
                    message = await bot.send_message(
                        chat_id=feed.channel_id,
                        text=text,
                        parse_mode="HTML",
                        link_preview_options=link_preview_options,
                    )
                    await self.repository.mark_article_posted(article.id, message.message_id)
                    posted += 1
                except Exception as exc:  # Telegram errors vary by API response.
                    logger.exception("Could not post article %s for feed %s", entry.link, feed_id)
                    failures.append(f"{entry.title}: {exc}")

            error = "; ".join(failures) if failures else None
            await self.repository.mark_success(
                feed_id,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                error=error,
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

