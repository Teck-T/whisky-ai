"""監視対象と定期収集のテスト (フィクスチャのみ、実通信なし)。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from whisky_ai import db, watchlist
from whisky_ai.collectors.base import Collector, SearchQuery, SearchResult
from whisky_ai.collectors.factory import build_collector
from whisky_ai.collectors.rakuten import CollectorError
from whisky_ai.sample_data import SampleCatalog


def test_v1_database_upgrades_to_v2(tmp_path: Path) -> None:
    """PR1 時点 (0001 のみ) の DB に 0002 だけが追加適用される。"""
    conn = db.connect(tmp_path / "old.sqlite3")
    first = db.load_migrations()[0]
    conn.executescript(first.sql)
    db.current_version(conn)
    with conn:
        conn.execute("INSERT INTO schema_migrations VALUES (1, ?, '2026-10-27T00:00:00Z')", (first.name,))
    assert db.migrate(conn) == ["0002_watch_targets.sql"]


def test_add_targets_for_bottle_prefers_japanese_name_and_yahoo_jan(
    conn: sqlite3.Connection, catalog: SampleCatalog
) -> None:
    bid = catalog.bottle_ids["Aokawa 12 Years 700ml"]
    ids = watchlist.add_targets_for_bottle(conn, bid)
    rows = {r["marketplace"]: r for r in conn.execute("SELECT * FROM watch_targets")}
    assert len(ids) == 2
    assert (rows["rakuten"]["keyword"], rows["rakuten"]["jan"]) == ("青川 12年", None)
    assert (rows["yahoo"]["keyword"], rows["yahoo"]["jan"]) == (None, catalog.jans["Aokawa 12 Years 700ml"])
    assert rows["yahoo"]["bottle_id"] == bid


def test_rakuten_target_requires_keyword(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="JAN"):
        watchlist.add_target(conn, "rakuten", label="x", jan="2900000000049")
    with pytest.raises(sqlite3.IntegrityError):  # DB の CHECK でも防ぐ
        conn.execute("INSERT INTO watch_targets (label, marketplace, jan, created_at)"
                     " VALUES ('x', 'rakuten', '1', 'now')")


def test_duplicate_target_rejected(conn: sqlite3.Connection) -> None:
    watchlist.add_target(conn, "yahoo", label="a", keyword="Aokawa")
    with pytest.raises(sqlite3.IntegrityError):
        watchlist.add_target(conn, "yahoo", label="b", keyword="Aokawa")


def test_due_targets_respects_interval_and_enabled(conn: sqlite3.Connection) -> None:
    a = watchlist.add_target(conn, "yahoo", label="a", keyword="A", min_interval_hours=24)
    b = watchlist.add_target(conn, "yahoo", label="b", keyword="B", min_interval_hours=6)
    c = watchlist.add_target(conn, "yahoo", label="c", keyword="C")
    watchlist.set_enabled(conn, c, False)
    conn.execute("UPDATE watch_targets SET last_run_at = '2026-10-28T00:00:00Z'")
    due = lambda now: [r["id"] for r in watchlist.due_targets(conn, now)]  # noqa: E731
    assert due("2026-10-28T05:59:59Z") == []
    assert due("2026-10-28T06:00:00Z") == [b]
    assert due("2026-10-29T00:00:00Z") == [a, b]
    with pytest.raises(KeyError):
        watchlist.set_enabled(conn, 999, True)


def test_run_due_with_mock_collects_and_records_status(
    conn: sqlite3.Connection, catalog: SampleCatalog
) -> None:
    watchlist.add_targets_for_bottle(conn, catalog.bottle_ids["Aokawa 12 Years 700ml"])
    report = watchlist.run_due(conn, force_mock=True, now="2026-10-28T00:00:00Z")
    assert report.ran == 2 and report.errors == []
    # 楽天フィクスチャに「青川 12年」を含む商品はない。Yahoo! は JAN で 1 件照合される
    assert report.match_status == {"matched": 1}
    assert {r[0] for r in conn.execute("SELECT last_status FROM watch_targets")} == {"ok"}
    # 間隔内の再実行では何もしない
    again = watchlist.run_due(conn, force_mock=True, now="2026-10-28T01:00:00Z")
    assert again.ran == 0
    assert watchlist.run_due(conn, force_mock=True, run_all=True).ran == 2


class _Failing:
    marketplace = "rakuten"
    is_mock = True
    source_title = "failing"
    source_url = "https://example.invalid/"

    def search(self, query: SearchQuery) -> SearchResult:
        raise CollectorError("Rakuten API HTTP 403: forbidden")


def test_run_due_continues_after_error(conn: sqlite3.Connection, catalog: SampleCatalog) -> None:
    watchlist.add_target(conn, "rakuten", label="r", keyword="Aokawa")
    watchlist.add_target(conn, "yahoo", label="y", keyword="Aokawa")

    def factory(source: watchlist.Source, mock: bool) -> Collector:
        return _Failing() if source == "rakuten" else build_collector(source, force_mock=True)

    report = watchlist.run_due(conn, collector_factory=factory)
    assert report.ran == 2 and len(report.errors) == 1 and "403" in report.errors[0]
    rows = {r["marketplace"]: r for r in conn.execute("SELECT * FROM watch_targets")}
    assert rows["rakuten"]["last_status"] == "error" and "403" in rows["rakuten"]["last_error"]
    assert rows["yahoo"]["last_status"] == "ok"


def test_single_instance_lock(tmp_path: Path) -> None:
    lock = tmp_path / "w.lock"
    results: list[bool] = []
    with watchlist.single_instance(lock) as first:
        t = threading.Thread(target=lambda: results.append(
            watchlist.single_instance(lock).__enter__()))
        t.start()
        t.join()
        assert first is True
    assert results == [False]
    with watchlist.single_instance(lock) as again:
        assert again is True
