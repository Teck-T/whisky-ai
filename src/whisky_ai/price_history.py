"""価格の推移と現在の最安値。

対象は照合確定済み (matched) の出品だけ。税込価格がわかり、在庫切れでない観測を使う。
送料は API から金額が得られないことが多いため、すべて「税込・送料別」で比べる。
日付の区切りは日本時間 (JST)。日時の計算は SQL ではなく Python で行う (PostgreSQL 移行を楽にするため)。
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
_TS = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, _TS).replace(tzinfo=UTC)


@dataclass(frozen=True)
class Observation:
    listing_id: int
    observed_at: datetime
    price_jpy: int
    marketplace: str
    shop_name: str | None
    item_url: str | None


@dataclass(frozen=True)
class DailyBest:
    day: date
    price_jpy: int
    listings: int  # その日に観測があった出品数
    shop_name: str | None
    marketplace: str


def observations(
    conn: sqlite3.Connection, bottle_id: int, since: datetime | None = None
) -> list[Observation]:
    # 「? IS NULL OR ...」は PostgreSQL で型を決められないため、条件ごと組み立てる
    since_sql = "AND p.observed_at >= ?" if since else ""
    params: tuple[object, ...] = (bottle_id, since.strftime(_TS)) if since else (bottle_id,)
    rows = conn.execute(
        f"""
        SELECT p.listing_id, p.observed_at, p.tax_included_price_jpy AS price, l.marketplace,
               s.name AS shop_name, l.item_url
        FROM price_observations p
        JOIN listings l ON l.id = p.listing_id
        LEFT JOIN shops s ON s.id = l.shop_id
        WHERE l.bottle_id = ? AND l.match_status = 'matched'
          AND p.tax_included_price_jpy IS NOT NULL AND COALESCE(p.in_stock, 1) = 1
          {since_sql}
        ORDER BY p.observed_at, p.id
        """,
        params,
    )
    return [Observation(r["listing_id"], parse_ts(r["observed_at"]), r["price"], r["marketplace"],
                        r["shop_name"], r["item_url"]) for r in rows]


def daily_best(obs: list[Observation]) -> list[DailyBest]:
    """日 (JST) ごとの最安値。"""
    by_day: dict[date, list[Observation]] = {}
    for o in obs:
        by_day.setdefault(o.observed_at.astimezone(JST).date(), []).append(o)
    out = []
    for day in sorted(by_day):
        items = by_day[day]
        best = min(items, key=lambda o: o.price_jpy)
        out.append(DailyBest(day, best.price_jpy, len({o.listing_id for o in items}), best.shop_name,
                             best.marketplace))
    return out


def history(conn: sqlite3.Connection, bottle_id: int, *, days: int = 90,
            now: datetime | None = None) -> list[DailyBest]:
    now = now or datetime.now(UTC)
    return daily_best(observations(conn, bottle_id, now - timedelta(days=days)))


def current_best(conn: sqlite3.Connection, bottle_id: int, *, max_age_hours: int = 72,
                 now: datetime | None = None) -> Observation | None:
    """各出品の最新観測のうち最安のもの。古い観測 (``max_age_hours`` 超) は使わない。

    最新観測が在庫切れ・税込不明の出品は除外する (古い在庫ありの観測にさかのぼらない)。
    """
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """
        SELECT p.listing_id, p.observed_at, p.tax_included_price_jpy AS price, p.in_stock,
               l.marketplace, s.name AS shop_name, l.item_url
        FROM listings l
        JOIN price_observations p ON p.listing_id = l.id
        LEFT JOIN shops s ON s.id = l.shop_id
        WHERE l.bottle_id = ? AND l.match_status = 'matched'
          AND p.id = (SELECT p2.id FROM price_observations p2 WHERE p2.listing_id = l.id
                      ORDER BY p2.observed_at DESC, p2.id DESC LIMIT 1)
        """,
        (bottle_id,),
    )
    best: Observation | None = None
    for r in rows:
        at = parse_ts(r["observed_at"])
        if r["price"] is None or r["in_stock"] == 0 or now - at > timedelta(hours=max_age_hours):
            continue
        o = Observation(r["listing_id"], at, r["price"], r["marketplace"], r["shop_name"], r["item_url"])
        if best is None or o.price_jpy < best.price_jpy:
            best = o
    return best


def reference_price(series: list[DailyBest], before: date, lookback_days: int,
                    min_days: int = 3) -> int | None:
    """``before`` より前の ``lookback_days`` 日間の日別最安値の中央値 (相場)。

    中央値を使うのは、一時的な特売や誤登録の極端な値に引っ張られないようにするため。
    データのある日が ``min_days`` 未満なら None (相場を判断できない)。
    """
    start = before - timedelta(days=lookback_days)
    window = [d.price_jpy for d in series if start <= d.day < before]
    if len(window) < min_days:
        return None
    return round(statistics.median(window))
