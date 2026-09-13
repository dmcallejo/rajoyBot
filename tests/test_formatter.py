from rss_to_tg.feed_parser import FeedEntry
from rss_to_tg.formatter import format_article


def test_format_article_escapes_html_and_keeps_preview_enabled() -> None:
    text, preview = format_article(
        FeedEntry(
            guid="1",
            link="https://example.test/?a=1&b=2",
            title="A <title>",
            summary="A useful summary",
            published_at=None,
        )
    )

    assert "A &lt;title&gt;" in text
    assert "A useful summary" in text
    assert "Read more" in text
    assert "https://example.test/?a=1&amp;b=2" in text
    assert preview.is_disabled is False

