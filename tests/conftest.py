from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterator

import pytest

from whisky_ai import db
from whisky_ai.matching import Catalog, Matcher
from whisky_ai.sample_data import SampleCatalog, load_sample_catalog

# 環境変数 WHISKY_AI_TEST_PG_URL があれば、conn を使うテストを PostgreSQL でも実行する
PG_URL = os.environ.get("WHISKY_AI_TEST_PG_URL")
BACKENDS = ["sqlite", "postgres"] if PG_URL else ["sqlite"]


@pytest.fixture(params=BACKENDS)
def conn(request: pytest.FixtureRequest) -> Iterator[sqlite3.Connection]:
    if request.param == "sqlite":
        c = db.connect(":memory:")
        db.migrate(c)
        yield c
        c.close()
        return
    # テストごとに使い捨てのスキーマを作り、終わったら消す
    from whisky_ai.pg import PgConnection

    assert PG_URL is not None
    schema = f"t_{uuid.uuid4().hex[:12]}"
    admin = PgConnection(PG_URL)
    admin.executescript(f"CREATE SCHEMA {schema}")
    admin.commit()
    pg = PgConnection(PG_URL, schema=schema)
    c = pg  # type: ignore[assignment]
    db.migrate(c)  # type: ignore[arg-type]
    try:
        yield c  # type: ignore[misc]
    finally:
        pg.close()
        admin.executescript(f"DROP SCHEMA {schema} CASCADE")
        admin.commit()
        admin.close()


@pytest.fixture
def catalog(conn: sqlite3.Connection) -> SampleCatalog:
    return load_sample_catalog(conn)


@pytest.fixture
def matcher(conn: sqlite3.Connection, catalog: SampleCatalog) -> Matcher:
    return Matcher(Catalog.load(conn))
