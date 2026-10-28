"""検索 → 生レスポンス保存 → 出品の登録 → 照合 → 価格観測の追記。"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from whisky_ai import repository as repo
from whisky_ai.collectors.base import Collector, SearchQuery
from whisky_ai.matching import Catalog, Matcher, resolve_listing

RETENTION_ENV = "WHISKY_AI_RAW_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 30


@dataclass
class CollectSummary:
    offers: int = 0
    observations: int = 0
    match_status: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)


def retention_days() -> int:
    """生レスポンスの保持日数。各 API の規約に保持期間の明記が見当たらないため
    (docs/api_research.md)、既定は短めの 30 日。0 なら生レスポンスを保存しない。"""
    return int(os.environ.get(RETENTION_ENV, DEFAULT_RETENTION_DAYS))


def _plus_days(ts: str, days: int) -> str:
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") + timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(
    conn: sqlite3.Connection, collector: Collector, query: SearchQuery,
    *, matcher: Matcher | None = None, keep_raw_days: int | None = None,
) -> CollectSummary:
    result = collector.search(query)
    matcher = matcher or Matcher(Catalog.load(conn))
    days = retention_days() if keep_raw_days is None else keep_raw_days
    summary = CollectSummary(offers=len(result.offers), warnings=list(result.warnings))
    marketplace = collector.marketplace
    with conn:
        source_id = repo.get_or_create_source(
            conn, "sample" if collector.is_mock else "api",
            collector.source_title + (" [mock fixture]" if collector.is_mock else ""),
            url=collector.source_url,
        )
        raw_id = None
        if days > 0:
            raw_id = repo.add_raw_response(
                conn, marketplace, result.raw.endpoint, result.raw.params, result.raw.status_code,
                result.raw.body, fetched_at=result.raw.fetched_at,
                expires_at=_plus_days(result.raw.fetched_at, days),
            )
        listing_ids = []
        for offer in result.offers:
            shop_id = None
            if offer.shop_external_id:
                shop_id = repo.upsert_shop(conn, marketplace, offer.shop_external_id,
                                           offer.shop_name or offer.shop_external_id, offer.shop_url)
            lid = repo.upsert_listing(conn, repo.ListingData(
                marketplace, offer.external_item_id, offer.title, item_url=offer.item_url,
                jan_raw=offer.jan_raw, shop_id=shop_id,
            ))
            repo.record_price_observation(conn, repo.PriceObservation(
                listing_id=lid, price_jpy=offer.price_jpy, source_id=source_id,
                observed_at=result.raw.fetched_at, price_tax_included=offer.price_tax_included,
                tax_included_price_jpy=offer.tax_included_price_jpy,
                shipping_fee_jpy=offer.shipping_fee_jpy, shipping_condition=offer.shipping_condition,
                in_stock=offer.in_stock, raw_response_id=raw_id,
            ))
            summary.observations += 1
            listing_ids.append(lid)
    # 照合は listing ごとに自前のトランザクションで保存する
    for lid in listing_ids:
        status = conn.execute("SELECT match_status FROM listings WHERE id = ?", (lid,)).fetchone()[0]
        if status == "unprocessed":
            res = resolve_listing(conn, lid, matcher)
            status = res.decision.value if res else status
        summary.match_status[status] += 1
    return summary
