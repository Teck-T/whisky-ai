"""監視対象 (watch_targets) の管理と定期収集。

``run_due`` は前回実行から ``min_interval_hours`` 以上たった対象だけを収集する。
cron / launchd から 1 日数回呼んでも、API へのリクエストは間隔どおりに抑えられる。
"""

from __future__ import annotations

import fcntl
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from whisky_ai.collectors.base import Collector, SearchQuery
from whisky_ai.collectors.factory import build_collector
from whisky_ai.collectors.pipeline import collect
from whisky_ai.db import utcnow
from whisky_ai.matching import Catalog, Matcher

Source = Literal["rakuten", "yahoo"]
_TS = "%Y-%m-%dT%H:%M:%SZ"


def add_target(
    conn: sqlite3.Connection, marketplace: Source, *, label: str, keyword: str | None = None,
    jan: str | None = None, bottle_id: int | None = None, min_interval_hours: int = 24,
) -> int:
    if marketplace == "rakuten" and not keyword:
        raise ValueError("Rakuten targets need a keyword (the API has no JAN search)")
    if not (keyword or jan):
        raise ValueError("keyword or jan is required")
    with conn:
        cur = conn.execute(
            "INSERT INTO watch_targets (bottle_id, label, marketplace, keyword, jan, min_interval_hours,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bottle_id, label, marketplace, keyword, jan, min_interval_hours, utcnow()),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def add_targets_for_bottle(
    conn: sqlite3.Connection, bottle_id: int, sources: tuple[Source, ...] = ("rakuten", "yahoo"),
    *, keyword: str | None = None, min_interval_hours: int = 24,
) -> list[int]:
    """カタログのボトルから監視対象を作る。

    キーワードは日本語名 (銘柄 + リリース) を優先する。日本の通販サイトの商品名は日本語が多いため。
    Yahoo! はボトルに JAN があれば JAN で検索する (名前より正確)。
    """
    row = conn.execute(
        """
        SELECT b.display_name, b.volume_ml, br.name AS brand, br.name_ja AS brand_ja,
               r.name AS rel, r.name_ja AS rel_ja
        FROM bottles b JOIN releases r ON r.id = b.release_id JOIN brands br ON br.id = r.brand_id
        WHERE b.id = ?
        """,
        (bottle_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"bottle {bottle_id} not found")
    if keyword is None:
        if row["brand_ja"] and row["rel_ja"]:
            keyword = f"{row['brand_ja']} {row['rel_ja']}"
        else:
            keyword = f"{row['brand']} {row['rel']}"
    jan_row = conn.execute(
        "SELECT value FROM product_identifiers WHERE bottle_id = ? ORDER BY id LIMIT 1", (bottle_id,)
    ).fetchone()
    ids = []
    for source in sources:
        jan = jan_row["value"] if source == "yahoo" and jan_row else None
        ids.append(add_target(
            conn, source, label=row["display_name"], bottle_id=bottle_id,
            keyword=None if jan else keyword, jan=jan, min_interval_hours=min_interval_hours,
        ))
    return ids


def set_enabled(conn: sqlite3.Connection, target_id: int, enabled: bool) -> None:
    with conn:
        cur = conn.execute("UPDATE watch_targets SET enabled = ? WHERE id = ?", (int(enabled), target_id))
    if cur.rowcount == 0:
        raise KeyError(f"watch target {target_id} not found")


def due_targets(conn: sqlite3.Connection, now: str | None = None) -> list[sqlite3.Row]:
    now_dt = datetime.strptime(now or utcnow(), _TS)
    due = []
    for row in conn.execute("SELECT * FROM watch_targets WHERE enabled = 1 ORDER BY id"):
        last = row["last_run_at"]
        if last is None or datetime.strptime(last, _TS) + timedelta(
                hours=row["min_interval_hours"]) <= now_dt:
            due.append(row)
    return due


@dataclass
class RunReport:
    ran: int = 0
    errors: list[str] = field(default_factory=list)
    match_status: dict[str, int] = field(default_factory=dict)


def run_due(
    conn: sqlite3.Connection, *, force_mock: bool = False, run_all: bool = False,
    now: str | None = None,
    collector_factory: Callable[[Source, bool], Collector] | None = None,
) -> RunReport:
    """期限の来た対象を収集する。1 件失敗しても残りは続け、エラーは対象ごとに記録する。

    Collector はマーケットプレイスごとに 1 つだけ作り使い回す (レート制限を共有するため)。
    """
    factory = collector_factory or (lambda s, mock: build_collector(s, force_mock=mock))
    targets = (list(conn.execute("SELECT * FROM watch_targets WHERE enabled = 1 ORDER BY id"))
               if run_all else due_targets(conn, now))
    report = RunReport()
    collectors: dict[Source, Collector] = {}
    matcher = Matcher(Catalog.load(conn))
    for t in targets:
        source: Source = t["marketplace"]
        collector = collectors.setdefault(source, factory(source, force_mock))
        status, error = "ok", None
        try:
            summary = collect(conn, collector, SearchQuery(keyword=t["keyword"], jan=t["jan"]),
                              matcher=matcher)
            for k, v in summary.match_status.items():
                report.match_status[k] = report.match_status.get(k, 0) + v
        except Exception as e:  # 1 件の失敗で全体を止めない
            status, error = "error", f"{type(e).__name__}: {e}"[:500]
            report.errors.append(f"[{t['id']}] {t['label']}: {error}")
        with conn:
            conn.execute(
                "UPDATE watch_targets SET last_run_at = ?, last_status = ?, last_error = ? WHERE id = ?",
                (now or utcnow(), status, error, t["id"]),
            )
        report.ran += 1
    return report


@contextmanager
def single_instance(lock_path: Path) -> Iterator[bool]:
    """同じ DB に対する定期収集の多重起動を防ぐ (cron と手動実行が重なった場合など)。

    取得できれば True、他のプロセスが実行中なら False を返す。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
