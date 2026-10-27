"""価格・定価の保存ルール。"""

from __future__ import annotations

import sqlite3

import pytest

from whisky_ai import repository as repo
from whisky_ai.matching import Matcher, resolve_listing
from whisky_ai.sample_data import SampleCatalog


@pytest.fixture
def listing_id(conn: sqlite3.Connection, catalog: SampleCatalog) -> int:
    with conn:
        shop = repo.upsert_shop(conn, "rakuten", "shop-a", "テスト酒店")
        return repo.upsert_listing(conn, repo.ListingData(
            "rakuten", "shop-a:1", "Aokawa 12年 700ml", item_url="https://example.invalid/1",
            shop_id=shop))


@pytest.fixture
def api_source(conn: sqlite3.Connection) -> int:
    with conn:
        return repo.add_source(conn, "api", "楽天市場商品検索API (mock)")


def test_msrp_requires_source(conn: sqlite3.Connection, catalog: SampleCatalog) -> None:
    bottle = catalog.bottle_ids["Aokawa 12 Years 700ml"]
    with pytest.raises(ValueError, match="requires a source"):
        repo.add_retail_price_reference(conn, bottle, 5000, tax_included=True, source_id=None)


def test_marketplace_price_cannot_be_msrp(
    conn: sqlite3.Connection, catalog: SampleCatalog, api_source: int
) -> None:
    bottle = catalog.bottle_ids["Aokawa 12 Years 700ml"]
    with pytest.raises(ValueError, match="not MSRP"):
        repo.add_retail_price_reference(conn, bottle, 5000, tax_included=True, source_id=api_source)


def test_msrp_and_market_prices_are_separate_tables(
    conn: sqlite3.Connection, catalog: SampleCatalog, listing_id: int, api_source: int
) -> None:
    repo.record_price_observation(conn, repo.PriceObservation(listing_id, 9999, api_source,
                                                              price_tax_included=True))
    bottle = catalog.bottle_ids["Aokawa 12 Years 700ml"]
    msrps = conn.execute(
        "SELECT price_jpy FROM retail_price_references WHERE bottle_id = ?", (bottle,)
    ).fetchall()
    assert 9999 not in {r[0] for r in msrps}


def test_tax_included_price_is_not_estimated(
    conn: sqlite3.Connection, listing_id: int, api_source: int
) -> None:
    incl = repo.record_price_observation(conn, repo.PriceObservation(
        listing_id, 5500, api_source, price_tax_included=True))
    excl = repo.record_price_observation(conn, repo.PriceObservation(
        listing_id, 5000, api_source, price_tax_included=False))
    explicit = repo.record_price_observation(conn, repo.PriceObservation(
        listing_id, 5000, api_source, price_tax_included=False, tax_included_price_jpy=5500))
    unknown = repo.record_price_observation(conn, repo.PriceObservation(listing_id, 5000, api_source))
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM price_observations")}
    assert rows[incl]["tax_included_price_jpy"] == 5500
    assert rows[excl]["tax_included_price_jpy"] is None
    assert rows[explicit]["tax_included_price_jpy"] == 5500
    assert rows[unknown]["price_tax_included"] is None


def test_shipping_rules(conn: sqlite3.Connection, listing_id: int, api_source: int) -> None:
    free = repo.record_price_observation(conn, repo.PriceObservation(
        listing_id, 5500, api_source, shipping_condition="free"))
    sep = repo.record_price_observation(conn, repo.PriceObservation(
        listing_id, 5500, api_source, shipping_condition="separate"))
    assert conn.execute("SELECT shipping_fee_jpy FROM price_observations WHERE id = ?",
                        (free,)).fetchone()[0] == 0
    assert conn.execute("SELECT shipping_fee_jpy FROM price_observations WHERE id = ?",
                        (sep,)).fetchone()[0] is None  # 送料額不明は NULL のまま
    with pytest.raises(ValueError):
        repo.record_price_observation(conn, repo.PriceObservation(
            listing_id, 5500, api_source, shipping_condition="free", shipping_fee_jpy=800))


def test_latest_prices_only_for_confirmed_matches(
    conn: sqlite3.Connection, catalog: SampleCatalog, matcher: Matcher, listing_id: int,
    api_source: int,
) -> None:
    bottle = catalog.bottle_ids["Aokawa 12 Years 700ml"]
    with conn:
        pending = repo.upsert_listing(conn, repo.ListingData("rakuten", "shop-a:2", "Aokawa 700ml"))
    for lid in (listing_id, pending):
        for ts, price in (("2026-10-01T00:00:00Z", 6000), ("2026-10-02T00:00:00Z", 5800)):
            repo.record_price_observation(conn, repo.PriceObservation(
                lid, price, api_source, observed_at=ts, price_tax_included=True, in_stock=True))
        resolve_listing(conn, lid, matcher)
    rows = repo.latest_prices_for_bottle(conn, bottle)
    assert [(r["listing_id"], r["price_jpy"], r["shop_name"]) for r in rows] == [
        (listing_id, 5800, "テスト酒店")]


def test_add_identifier_rejects_invalid(conn: sqlite3.Connection, catalog: SampleCatalog) -> None:
    with pytest.raises(ValueError):
        repo.add_identifier(conn, catalog.bottle_ids["Aokawa 12 Years 700ml"], "4901777018687",
                            catalog.source_id)


def test_sample_jans_are_instore_codes(catalog: SampleCatalog) -> None:
    """架空データの JAN は GS1 インストアコード ('2' 始まり) で実在商品と衝突しない。"""
    assert all(j.startswith("2") for j in catalog.jans.values())


def test_upsert_shop_is_idempotent(conn: sqlite3.Connection) -> None:
    a = repo.upsert_shop(conn, "yahoo", "s1", "A")
    b = repo.upsert_shop(conn, "yahoo", "s1", "A2")
    assert a == b
    assert conn.execute("SELECT name FROM shops WHERE id = ?", (a,)).fetchone()[0] == "A2"
