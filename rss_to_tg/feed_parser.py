from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from time import struct_time
from urllib.parse import urljoin

import feedparser


@dataclass(frozen=True, slots=True)
class FeedEntry:
    guid: str
    link: str
    title: str
    summary: str
    published_at: datetime | None


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _entry_date(entry) -> datetime | None:
    parsed: struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC).replace(tzinfo=None)


def parse_feed(content: bytes | str, feed_url: str) -> list[FeedEntry]:
    """Parse RSS/Atom content into normalized, deduplicatable entries."""
    parsed = feedparser.parse(content)
    entries: list[FeedEntry] = []
    for raw in parsed.entries:
        raw_link = str(raw.get("link") or "").strip()
        link = urljoin(feed_url, raw_link)
        if not link or link == feed_url:
            continue

        title = _clean_text(raw.get("title")) or "Untitled article"
        summary = _clean_text(raw.get("summary") or raw.get("description"))
        if not summary and raw.get("content"):
            summary = _clean_text(raw.content[0].get("value"))
        raw_guid = str(raw.get("id") or raw.get("guid") or link).strip()
        guid = raw_guid or hashlib.sha256(link.encode("utf-8")).hexdigest()
        entries.append(
            FeedEntry(
                guid=guid[:2048],
                link=link,
                title=title[:1024],
                summary=summary,
                published_at=_entry_date(raw),
            )
        )
    return entries

