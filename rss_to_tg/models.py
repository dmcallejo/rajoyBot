from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.utcnow()


class Feed(Base):
    __tablename__ = "feeds"
    __table_args__ = (
        Index("ix_feeds_enabled_last_checked", "enabled", "last_checked_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(255), nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    title_override: Mapped[str | None] = mapped_column(String(255), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    articles: Mapped[list["Article"]] = relationship(
        back_populates="feed", cascade="all, delete-orphan", passive_deletes=True
    )
    configurations: Mapped[list["FeedConfiguration"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )
    url_key: Mapped["FeedURLKey | None"] = relationship(
        back_populates="source", cascade="all, delete-orphan", uselist=False, passive_deletes=True
    )


class FeedURLKey(Base):
    """Compact unique key for shared URLs, including on databases with index limits."""

    __tablename__ = "feed_url_keys"

    url_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    feed_id: Mapped[int] = mapped_column(
        ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    source: Mapped[Feed] = relationship(back_populates="url_key")


class FeedConfiguration(Base):
    """A private, named route from one shared feed to one Telegram channel."""

    __tablename__ = "feed_configurations"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_configuration_user_name"),
        Index("ix_configurations_source_enabled", "feed_id", "enabled"),
        Index("ix_configurations_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    feed_id: Mapped[int] = mapped_column(
        ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel_id: Mapped[str] = mapped_column(String(255), nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    feed_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    source: Mapped[Feed] = relationship(back_populates="configurations")
    deliveries: Mapped[list["Delivery"]] = relationship(
        back_populates="configuration", cascade="all, delete-orphan", passive_deletes=True
    )


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("feed_id", "guid", name="uq_article_feed_guid"),
        Index("ix_articles_feed_posted", "feed_id", "posted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feed_id: Mapped[int] = mapped_column(
        ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    guid: Mapped[str] = mapped_column(String(2048), nullable=False)
    link: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    feed: Mapped[Feed] = relationship(back_populates="articles")
    deliveries: Mapped[list["Delivery"]] = relationship(
        back_populates="article", cascade="all, delete-orphan", passive_deletes=True
    )


class Delivery(Base):
    """Per-configuration posting state for an article discovered by a shared feed poll."""

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("configuration_id", "article_id", name="uq_delivery_configuration_article"),
        Index("ix_deliveries_configuration_posted", "configuration_id", "posted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("feed_configurations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    configuration: Mapped[FeedConfiguration] = relationship(back_populates="deliveries")
    article: Mapped[Article] = relationship(back_populates="deliveries")
