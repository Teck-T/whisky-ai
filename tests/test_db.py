from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from whisky_ai import db

EXPECTED_TABLES = {
    "sources", "distilleries", "brands", "brand_distilleries", "releases", "batches", "bottles",
    "product_identifiers", "aliases", "retail_price_references", "shops", "listings",
    "match_candidates", "api_raw_responses", "price_observations", "reviews",
    "flavor_extractions", "flavor_notes", "flavor_axes", "schema_migrations", "watch_targets",
}


def test_migrate_creates_all_tables_and_is_idempotent(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.sqlite3")
    assert db.migrate(conn) == ["0001_initial.sql", "0002_watch_targets.sql"]
    assert db.migrate(conn) == []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables >= EXPECTED_TABLES
    assert db.current_version(conn) == 2


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO brands (name, category, source_id, created_at) VALUES ('x', 'blended', 999, 'now')"
        )


def test_listing_matched_requires_bottle(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO listings (marketplace, external_item_id, title, match_status, first_seen_at,"
            " last_seen_at) VALUES ('mock', 'a', 't', 'matched', 'now', 'now')"
        )


def test_bottle_spec_unique_even_without_batch(conn: sqlite3.Connection, catalog: object) -> None:
    row = conn.execute("SELECT * FROM bottles WHERE batch_id IS NULL LIMIT 1").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bottles (release_id, volume_ml, abv_bp, packaging, display_name, source_id,"
            " created_at) VALUES (?, ?, ?, ?, 'dup', ?, 'now')",
            (row["release_id"], row["volume_ml"], row["abv_bp"], row["packaging"], row["source_id"]),
        )


def test_resolve_db_path_uses_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(db.DEFAULT_DB_ENV, str(tmp_path / "x.db"))
    assert db.resolve_db_path() == tmp_path / "x.db"
