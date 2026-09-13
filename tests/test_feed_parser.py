from rss_to_tg.feed_parser import parse_feed


def test_parse_rss_normalizes_html_and_relative_links() -> None:
    xml = b"""
    <rss version="2.0"><channel><title>Example</title>
      <item>
        <guid>article-1</guid><title><![CDATA[ Hello <b>world</b> ]]></title>
        <link>/posts/1</link>
        <description><![CDATA[<p>A <strong>short</strong> summary.</p>]]></description>
        <pubDate>Tue, 20 Aug 2024 10:00:00 GMT</pubDate>
      </item>
    </channel></rss>
    """
    entries = parse_feed(xml, "https://example.test/rss.xml")

    assert len(entries) == 1
    assert entries[0].guid == "article-1"
    assert entries[0].link == "https://example.test/posts/1"
    assert entries[0].title == "Hello world"
    assert entries[0].summary == "A short summary."
    assert entries[0].published_at is not None

