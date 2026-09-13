from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .database import Database
from .models import Article, Feed, utcnow


class DuplicateFeedError(ValueError):
    """Raised when the same feed is already connected to a channel."""


class FeedRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def list_feeds(self) -> list[Feed]:
        async with self.database.session() as session:
            result = await session.scalars(select(Feed).order_by(Feed.id))
            return list(result)

    async def get_feed(self, feed_id: int) -> Feed | None:
        async with self.database.session() as session:
            return await session.get(Feed, feed_id)

    async def create_feed(
        self,
        *,
        url: str,
        channel_id: str,
        interval_minutes: int,
        created_by: int,
    ) -> Feed:
        async with self.database.session() as session:
            feed = Feed(
                url=url,
                channel_id=channel_id,
                interval_minutes=interval_minutes,
                created_by=created_by,
            )
            session.add(feed)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateFeedError from exc
            await session.refresh(feed)
            return feed

    async def update_feed(self, feed_id: int, **values) -> Feed | None:
        async with self.database.session() as session:
            feed = await session.get(Feed, feed_id)
            if feed is None:
                return None
            for key, value in values.items():
                if not hasattr(feed, key):
                    raise ValueError(f"Unknown feed field: {key}")
                setattr(feed, key, value)
            feed.updated_at = utcnow()
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateFeedError from exc
            await session.refresh(feed)
            return feed

    async def delete_feed(self, feed_id: int) -> bool:
        async with self.database.session() as session:
            result = await session.execute(delete(Feed).where(Feed.id == feed_id))
            await session.commit()
            return result.rowcount > 0

    async def due_feeds(self, now: datetime | None = None) -> list[Feed]:
        now = now or utcnow()
        async with self.database.session() as session:
            result = await session.scalars(
                select(Feed)
                .where(Feed.enabled.is_(True))
                .order_by(Feed.id)
            )
            feeds = list(result)
            return [
                feed
                for feed in feeds
                if feed.last_checked_at is None
                or feed.last_checked_at <= now - timedelta(minutes=feed.interval_minutes)
            ]

    async def mark_checked(self, feed_id: int, checked_at: datetime | None = None) -> bool:
        async with self.database.session() as session:
            feed = await session.get(Feed, feed_id)
            if feed is None:
                return False
            feed.last_checked_at = checked_at or utcnow()
            feed.updated_at = utcnow()
            await session.commit()
            return True

    async def mark_success(
        self,
        feed_id: int,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self.database.session() as session:
            feed = await session.get(Feed, feed_id)
            if feed is None:
                return
            if etag is not None:
                feed.etag = etag
            if last_modified is not None:
                feed.last_modified = last_modified
            feed.last_success_at = utcnow()
            feed.last_error = error
            feed.updated_at = utcnow()
            await session.commit()

    async def mark_error(self, feed_id: int, error: str) -> None:
        async with self.database.session() as session:
            feed = await session.get(Feed, feed_id)
            if feed is None:
                return
            feed.last_error = error[:4000]
            feed.updated_at = utcnow()
            await session.commit()

    async def get_or_create_article(
        self,
        *,
        feed_id: int,
        guid: str,
        link: str,
        title: str,
        summary: str,
        published_at: datetime | None,
        skip: bool,
    ) -> tuple[Article, bool]:
        async with self.database.session() as session:
            article = await session.scalar(
                select(Article).where(Article.feed_id == feed_id, Article.guid == guid)
            )
            if article is not None:
                return article, False

            article = Article(
                feed_id=feed_id,
                guid=guid,
                link=link,
                title=title,
                summary=summary,
                published_at=published_at,
                skipped=skip,
            )
            session.add(article)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                article = await session.scalar(
                    select(Article).where(Article.feed_id == feed_id, Article.guid == guid)
                )
                if article is None:
                    raise
                return article, False
            await session.refresh(article)
            return article, True

    async def mark_article_posted(self, article_id: int, message_id: int) -> None:
        async with self.database.session() as session:
            article = await session.get(Article, article_id)
            if article is None:
                return
            article.posted_at = utcnow()
            article.telegram_message_id = message_id
            await session.commit()

    async def article_stats(self) -> tuple[int, int]:
        async with self.database.session() as session:
            total = await session.scalar(select(func.count(Article.id)))
            posted = await session.scalar(
                select(func.count(Article.id)).where(Article.posted_at.is_not(None))
            )
            return int(total or 0), int(posted or 0)
