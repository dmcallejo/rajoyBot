from __future__ import annotations

from html import escape

from telegram import LinkPreviewOptions

from .feed_parser import FeedEntry


def _shorten(value: str, length: int) -> str:
    value = value.strip()
    if len(value) <= length:
        return value
    return value[: length - 1].rsplit(" ", 1)[0].rstrip() + "…"


def format_article(entry: FeedEntry) -> tuple[str, LinkPreviewOptions]:
    title = escape(_shorten(entry.title, 240))
    summary = _shorten(entry.summary, 500)
    body = escape(summary) if summary else "New article available."
    text = f'<b>{title}</b>\n\n{body}\n\n<a href="{escape(entry.link, quote=True)}">Read more</a>'
    # A normal, enabled Telegram link preview is intentional. The article URL is
    # also the visible "Read more" anchor, so Telegram can generate its preview.
    return text, LinkPreviewOptions(is_disabled=False)

