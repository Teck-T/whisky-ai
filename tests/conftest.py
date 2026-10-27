from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from whisky_ai import db
from whisky_ai.matching import Catalog, Matcher
from whisky_ai.sample_data import SampleCatalog, load_sample_catalog


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def catalog(conn: sqlite3.Connection) -> SampleCatalog:
    return load_sample_catalog(conn)


@pytest.fixture
def matcher(conn: sqlite3.Connection, catalog: SampleCatalog) -> Matcher:
    return Matcher(Catalog.load(conn))
