from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from .database import Database
from .models import Article, Delivery, Feed, FeedConfiguration, FeedURLKey, utcnow


class DuplicateConfigurationError(ValueError):
    """Raised when a user already has a configuration with that name."""


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class FeedRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def migrate_legacy_data(self) -> None:
        """Turn pre-multiuser feed rows into private routes and merge duplicate polls."""
        async with self.database.session() as session:
            sources = list(await session.scalars(select(Feed).order_by(Feed.id)))
            configurations = list(await session.scalars(select(FeedConfiguration)))
            migrate_legacy = bool(sources and not configurations)
            if migrate_legacy:
                # Before this migration, each row represented both a channel route
                # and its own poll. Preserve each route while consolidating HTTP
                # state and article history by URL.
                grouped: dict[str, list[Feed]] = {}
                for source in sources:
                    grouped.setdefault(source.url, []).append(source)

                configuration_by_source: dict[int, FeedConfiguration] = {}
                for source in sources:
                    configuration = FeedConfiguration(
                        user_id=source.created_by,
                        name=f"legacy-{source.id}",
                        feed_id=source.id,
                        channel_id=source.channel_id,
                        interval_minutes=source.interval_minutes,
                        enabled=source.enabled,
                        created_at=source.created_at,
                        feed_started_at=source.created_at,
                        updated_at=source.updated_at,
                    )
                    session.add(configuration)
                    configuration_by_source[source.id] = configuration
                await session.flush()

                for same_url in grouped.values():
                    canonical = same_url[0]
                    article_by_guid = {
                        article.guid: article
                        for article in await session.scalars(
                            select(Article).where(Article.feed_id == canonical.id)
                        )
                    }
                    for old_source in same_url:
                        old_articles = list(
                            await session.scalars(
                                select(Article).where(Article.feed_id == old_source.id).order_by(Article.id)
                            )
                        )
                        for old_article in old_articles:
                            target = article_by_guid.get(old_article.guid)
                            if target is None:
                                target = Article(
                                    feed_id=canonical.id,
                                    guid=old_article.guid,
                                    link=old_article.link,
                                    title=old_article.title,
                                    summary=old_article.summary,
                                    published_at=old_article.published_at,
                                    seen_at=old_article.seen_at,
                                    posted_at=old_article.posted_at,
                                    skipped=old_article.skipped,
                                    telegram_message_id=old_article.telegram_message_id,
                                )
                                session.add(target)
                                await session.flush()
                                article_by_guid[target.guid] = target
                            session.add(
                                Delivery(
                                    configuration_id=configuration_by_source[old_source.id].id,
                                    article_id=target.id,
                                    posted_at=old_article.posted_at,
                                    skipped=old_article.skipped,
                                    telegram_message_id=old_article.telegram_message_id,
                                )
                            )

                        if old_source.id != canonical.id:
                            await session.execute(
                                update(FeedConfiguration)
                                .where(FeedConfiguration.feed_id == old_source.id)
                                .values(feed_id=canonical.id)
                            )
                            canonical.last_checked_at = max(
                                (value for value in (canonical.last_checked_at, old_source.last_checked_at) if value),
                                default=None,
                            )
                            canonical.last_success_at = max(
                                (value for value in (canonical.last_success_at, old_source.last_success_at) if value),
                                default=None,
                            )
                            if old_source.last_success_at and (
                                canonical.last_success_at == old_source.last_success_at
                            ):
                                canonical.etag = old_source.etag or canonical.etag
                                canonical.last_modified = old_source.last_modified or canonical.last_modified
                            await session.execute(delete(Article).where(Article.feed_id == old_source.id))
                            await session.execute(delete(Feed).where(Feed.id == old_source.id))

            live_sources = list(await session.scalars(select(Feed)))
            if migrate_legacy:
                for source in live_sources:
                    # These columns described the old single-channel route. Once
                    # copied into its private configuration, keep them off the
                    # shared source row.
                    source.channel_id = "shared"
                    source.created_by = 0
                    source.interval_minutes = 30
                    source.enabled = True
                    source.title_override = None
                    source.last_error = None
                    source.updated_at = utcnow()
                source_ids = [source.id for source in live_sources]
                if source_ids:
                    await session.execute(
                        update(Article)
                        .where(Article.feed_id.in_(source_ids))
                        .values(posted_at=None, skipped=False, telegram_message_id=None)
                    )
            keyed_ids = set(await session.scalars(select(FeedURLKey.feed_id)))
            for source in live_sources:
                if source.id not in keyed_ids:
                    session.add(FeedURLKey(url_hash=_url_hash(source.url), feed_id=source.id))
            await session.commit()

    async def list_configurations(self, user_id: int) -> list[FeedConfiguration]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(FeedConfiguration)
                .options(selectinload(FeedConfiguration.source))
                .where(FeedConfiguration.user_id == user_id)
                .order_by(FeedConfiguration.id)
            )
            return list(result)

    async def get_configuration(
        self, configuration_id: int, user_id: int
    ) -> FeedConfiguration | None:
        async with self.database.session() as session:
            return await session.scalar(
                select(FeedConfiguration)
                .options(selectinload(FeedConfiguration.source))
                .where(
                    FeedConfiguration.id == configuration_id,
                    FeedConfiguration.user_id == user_id,
                )
            )

    async def _source_for_url(self, url: str) -> Feed:
        source_conflict: IntegrityError | None = None
        async with self.database.session() as session:
            source = await session.scalar(select(Feed).where(Feed.url == url))
            if source is not None:
                return source
            source = Feed(
                url=url,
                channel_id="shared",
                interval_minutes=30,
                enabled=True,
                created_by=0,
            )
            session.add(source)
            try:
                await session.flush()
                session.add(FeedURLKey(url_hash=_url_hash(url), feed_id=source.id))
                await session.commit()
                await session.refresh(source)
                return source
            except IntegrityError as exc:
                await session.rollback()
                source_conflict = exc

        # Another concurrent command may have created the unique shared source.
        async with self.database.session() as session:
            source = await session.scalar(select(Feed).where(Feed.url == url))
            if source is None:
                if source_conflict is not None:
                    raise source_conflict
                raise RuntimeError("Could not create or find the shared feed source")
            return source

    async def create_configuration(
        self,
        *,
        user_id: int,
        name: str,
        url: str,
        channel_id: str,
        interval_minutes: int,
    ) -> FeedConfiguration:
        source = await self._source_for_url(url)
        async with self.database.session() as session:
            configuration = FeedConfiguration(
                user_id=user_id,
                name=name.strip(),
                feed_id=source.id,
                channel_id=channel_id,
                interval_minutes=interval_minutes,
            )
            session.add(configuration)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                await self._delete_orphan_source(source.id)
                raise DuplicateConfigurationError from exc
            await session.refresh(configuration)
            return configuration

    async def update_configuration(
        self, configuration_id: int, user_id: int, **values
    ) -> FeedConfiguration | None:
        allowed = {"name", "url", "channel_id", "interval_minutes", "enabled"}
        if not values.keys() <= allowed:
            raise ValueError("Unknown configuration field")

        async with self.database.session() as session:
            configuration = await session.scalar(
                select(FeedConfiguration).where(
                    FeedConfiguration.id == configuration_id,
                    FeedConfiguration.user_id == user_id,
                )
            )
            if configuration is None:
                return None

            old_source_id = configuration.feed_id
            new_url = values.pop("url", None)
            if new_url is not None:
                source = await session.scalar(select(Feed).where(Feed.url == new_url))
                if source is None:
                    source = Feed(
                        url=new_url,
                        channel_id="shared",
                        interval_minutes=30,
                        enabled=True,
                        created_by=0,
                    )
                    session.add(source)
                    await session.flush()
                    session.add(
                        FeedURLKey(url_hash=_url_hash(new_url), feed_id=source.id)
                    )
                if source.id != old_source_id:
                    configuration.feed_started_at = utcnow()
                configuration.feed_id = source.id
            for key, value in values.items():
                setattr(configuration, key, value)
            configuration.updated_at = utcnow()
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateConfigurationError from exc
            await session.refresh(configuration)

        if new_url is not None and old_source_id != configuration.feed_id:
            await self._delete_orphan_source(old_source_id)
        return await self.get_configuration(configuration_id, user_id)

    async def delete_configuration(self, configuration_id: int, user_id: int) -> bool:
        async with self.database.session() as session:
            configuration = await session.scalar(
                select(FeedConfiguration).where(
                    FeedConfiguration.id == configuration_id,
                    FeedConfiguration.user_id == user_id,
                )
            )
            if configuration is None:
                return False
            source_id = configuration.feed_id
            await session.delete(configuration)
            await session.commit()
        await self._delete_orphan_source(source_id)
        return True

    async def _delete_orphan_source(self, source_id: int) -> None:
        async with self.database.session() as session:
            source = await session.get(Feed, source_id)
            if source is None:
                return
            has_configurations = await session.scalar(
                select(FeedConfiguration.id)
                .where(FeedConfiguration.feed_id == source_id)
                .limit(1)
            )
            if has_configurations is None:
                await session.delete(source)
                await session.commit()

    async def due_feeds(self, now: datetime | None = None) -> list[Feed]:
        now = now or utcnow()
        async with self.database.session() as session:
            result = await session.execute(
                select(Feed, FeedConfiguration.interval_minutes)
                .join(FeedConfiguration, FeedConfiguration.feed_id == Feed.id)
                .where(FeedConfiguration.enabled.is_(True))
                .order_by(Feed.id)
            )
            source_intervals: dict[int, tuple[Feed, int]] = {}
            for source, interval_minutes in result:
                current = source_intervals.get(source.id)
                if current is None or interval_minutes < current[1]:
                    source_intervals[source.id] = (source, interval_minutes)
            due: list[Feed] = []
            for source, interval_minutes in source_intervals.values():
                if (
                    source.last_checked_at is None
                    or source.last_checked_at <= now - timedelta(minutes=interval_minutes)
                ):
                    due.append(source)
            return due

    async def get_source(self, source_id: int) -> Feed | None:
        async with self.database.session() as session:
            return await session.get(Feed, source_id)

    async def active_configurations(self, source_id: int) -> list[FeedConfiguration]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(FeedConfiguration)
                .where(
                    FeedConfiguration.feed_id == source_id,
                    FeedConfiguration.enabled.is_(True),
                )
                .order_by(FeedConfiguration.id)
            )
            return list(result)

    async def mark_checked(self, source_id: int, checked_at: datetime | None = None) -> bool:
        async with self.database.session() as session:
            source = await session.get(Feed, source_id)
            if source is None:
                return False
            source.last_checked_at = checked_at or utcnow()
            source.updated_at = utcnow()
            await session.commit()
            return True

    async def mark_success(
        self,
        source_id: int,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self.database.session() as session:
            source = await session.get(Feed, source_id)
            if source is None:
                return
            if etag is not None:
                source.etag = etag
            if last_modified is not None:
                source.last_modified = last_modified
            source.last_success_at = utcnow()
            source.last_error = error
            source.updated_at = utcnow()
            await session.commit()

    async def mark_error(self, source_id: int, error: str) -> None:
        async with self.database.session() as session:
            source = await session.get(Feed, source_id)
            if source is None:
                return
            source.last_error = error[:4000]
            source.updated_at = utcnow()
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

    async def get_or_create_delivery(
        self, *, configuration_id: int, article_id: int, skipped: bool
    ) -> tuple[Delivery, bool]:
        async with self.database.session() as session:
            delivery = await session.scalar(
                select(Delivery).where(
                    Delivery.configuration_id == configuration_id,
                    Delivery.article_id == article_id,
                )
            )
            if delivery is not None:
                return delivery, False
            delivery = Delivery(
                configuration_id=configuration_id,
                article_id=article_id,
                skipped=skipped,
            )
            session.add(delivery)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                delivery = await session.scalar(
                    select(Delivery).where(
                        Delivery.configuration_id == configuration_id,
                        Delivery.article_id == article_id,
                    )
                )
                if delivery is None:
                    raise
                return delivery, False
            await session.refresh(delivery)
            return delivery, True

    async def pending_deliveries(
        self, source_id: int
    ) -> list[tuple[Delivery, Article, FeedConfiguration]]:
        async with self.database.session() as session:
            result = await session.execute(
                select(Delivery, Article, FeedConfiguration)
                .join(Article, Delivery.article_id == Article.id)
                .join(FeedConfiguration, Delivery.configuration_id == FeedConfiguration.id)
                .where(
                    FeedConfiguration.feed_id == source_id,
                    FeedConfiguration.enabled.is_(True),
                    Article.feed_id == source_id,
                    Delivery.posted_at.is_(None),
                    Delivery.skipped.is_(False),
                )
                .order_by(Article.published_at, Article.id, FeedConfiguration.id)
            )
            return list(result)

    async def mark_delivery_posted(self, delivery_id: int, message_id: int) -> None:
        async with self.database.session() as session:
            delivery = await session.get(Delivery, delivery_id)
            if delivery is None:
                return
            delivery.posted_at = utcnow()
            delivery.telegram_message_id = message_id
            delivery.last_error = None
            await session.commit()

    async def mark_delivery_error(self, delivery_id: int, error: str) -> None:
        async with self.database.session() as session:
            delivery = await session.get(Delivery, delivery_id)
            if delivery is None:
                return
            delivery.last_error = error[:4000]
            await session.commit()

    async def user_stats(self, user_id: int) -> tuple[int, int, int]:
        async with self.database.session() as session:
            configurations = await session.scalar(
                select(func.count(FeedConfiguration.id)).where(
                    FeedConfiguration.user_id == user_id
                )
            )
            enabled = await session.scalar(
                select(func.count(FeedConfiguration.id)).where(
                    FeedConfiguration.user_id == user_id,
                    FeedConfiguration.enabled.is_(True),
                )
            )
            posted = await session.scalar(
                select(func.count(Delivery.id))
                .join(FeedConfiguration, Delivery.configuration_id == FeedConfiguration.id)
                .where(
                    FeedConfiguration.user_id == user_id,
                    Delivery.posted_at.is_not(None),
                )
            )
            return int(configurations or 0), int(enabled or 0), int(posted or 0)

    async def configuration_errors(self, user_id: int) -> dict[int, str]:
        """Return each user's latest delivery error without exposing peer routes."""
        async with self.database.session() as session:
            result = await session.execute(
                select(Delivery.configuration_id, Delivery.last_error)
                .join(
                    FeedConfiguration,
                    FeedConfiguration.id == Delivery.configuration_id,
                )
                .where(
                    FeedConfiguration.user_id == user_id,
                    Delivery.last_error.is_not(None),
                )
                .order_by(Delivery.id.desc())
            )
            errors: dict[int, str] = {}
            for configuration_id, error in result:
                errors.setdefault(configuration_id, error)
            return errors
