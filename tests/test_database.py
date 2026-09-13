from sqlalchemy import create_engine, inspect

from rss_to_tg.database import normalize_database_url
from rss_to_tg.models import Base


def test_database_url_normalization() -> None:
    assert normalize_database_url("sqlite:///./data.db").startswith("sqlite+aiosqlite:///")
    assert normalize_database_url("mysql://user:pass@host/db") == "mysql+aiomysql://user:pass@host/db"
    assert normalize_database_url("mariadb://user:pass@host/db") == "mysql+aiomysql://user:pass@host/db"


def test_orm_schema_creates_feed_and_article_tables(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    try:
        assert set(inspect(engine).get_table_names()) == {"feeds", "articles"}
    finally:
        engine.dispose()
