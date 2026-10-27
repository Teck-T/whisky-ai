"""マスター・販売商品・価格の書き込み API。

SQL はここに集約し、呼び出し側 (CLI・将来の価格収集モジュール) は
このモジュールの関数だけを使う。PostgreSQL 移行時の差し替え範囲を限定するため。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal

from whisky_ai.db import utcnow
from whisky_ai.normalize import normalize_batch_code, normalize_text, parse_gtin, parse_title

SourceKind = Literal["official_site", "press_release", "api", "retailer_page", "manual_entry", "sample"]
Marketplace = Literal["rakuten", "yahoo", "mock", "manual"]
ShippingCondition = Literal["included", "free", "conditional_free", "separate", "unknown"]

# DB やログに残してはいけないリクエストパラメータ名
SECRET_PARAM_NAMES = frozenset({"applicationId", "accessKey", "affiliateId", "appid"})


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, object]) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    assert cur.lastrowid is not None
    return cur.lastrowid


def add_source(
    conn: sqlite3.Connection, kind: SourceKind, title: str, *, url: str | None = None,
    publisher: str | None = None, retrieved_at: str | None = None, note: str | None = None,
) -> int:
    return _insert(conn, "sources", {
        "kind": kind, "title": title, "url": url, "publisher": publisher,
        "retrieved_at": retrieved_at or utcnow(), "note": note,
    })


def add_distillery(
    conn: sqlite3.Connection, name: str, country: str, source_id: int, *,
    name_ja: str | None = None, region: str | None = None, status: str = "active",
) -> int:
    return _insert(conn, "distilleries", {
        "name": name, "name_ja": name_ja, "country": country, "region": region,
        "status": status, "source_id": source_id, "created_at": utcnow(),
    })


def add_brand(
    conn: sqlite3.Connection, name: str, category: str, source_id: int, *,
    name_ja: str | None = None, owner: str | None = None,
    distillery_ids: tuple[int, ...] = (),
) -> int:
    brand_id = _insert(conn, "brands", {
        "name": name, "name_ja": name_ja, "owner": owner, "category": category,
        "source_id": source_id, "created_at": utcnow(),
    })
    for did in distillery_ids:
        role = "producer" if len(distillery_ids) == 1 else "component"
        conn.execute(
            "INSERT INTO brand_distilleries (brand_id, distillery_id, role) VALUES (?, ?, ?)",
            (brand_id, did, role),
        )
    return brand_id


def add_release(
    conn: sqlite3.Connection, brand_id: int, name: str, source_id: int, *,
    name_ja: str | None = None, age_years: int | None = None, vintage_year: int | None = None,
    edition: str | None = None, cask_type: str | None = None, is_limited: bool = False,
    has_batches: bool = False,
) -> int:
    return _insert(conn, "releases", {
        "brand_id": brand_id, "name": name, "name_ja": name_ja, "age_years": age_years,
        "vintage_year": vintage_year, "edition": edition, "cask_type": cask_type,
        "is_limited": int(is_limited), "has_batches": int(has_batches),
        "source_id": source_id, "created_at": utcnow(),
    })


def add_batch(
    conn: sqlite3.Connection, release_id: int, batch_code: str, source_id: int, *,
    batch_label: str | None = None, bottling_year: int | None = None,
    cask_number: str | None = None, outturn: int | None = None,
) -> int:
    return _insert(conn, "batches", {
        "release_id": release_id, "batch_code": normalize_batch_code(batch_code),
        "batch_label": batch_label or batch_code, "bottling_year": bottling_year,
        "cask_number": cask_number, "outturn": outturn, "source_id": source_id,
        "created_at": utcnow(),
    })


def add_bottle(
    conn: sqlite3.Connection, release_id: int, volume_ml: int, abv_bp: int, display_name: str,
    source_id: int, *, batch_id: int | None = None, packaging: str = "standard",
) -> int:
    return _insert(conn, "bottles", {
        "release_id": release_id, "batch_id": batch_id, "volume_ml": volume_ml,
        "abv_bp": abv_bp, "packaging": packaging, "display_name": display_name,
        "source_id": source_id, "created_at": utcnow(),
    })


def add_identifier(conn: sqlite3.Connection, bottle_id: int, raw_code: str, source_id: int) -> int:
    """JAN/EAN/UPC を登録。チェックディジット不正は ValueError (不正値を保存しない)。"""
    gtin = parse_gtin(raw_code)
    if gtin is None:
        raise ValueError(f"invalid GTIN: {raw_code!r}")
    return _insert(conn, "product_identifiers", {
        "bottle_id": bottle_id, "id_type": gtin.id_type, "value": gtin.value,
        "source_id": source_id, "created_at": utcnow(),
    })


def add_alias(
    conn: sqlite3.Connection, entity_type: Literal["distillery", "brand", "release"],
    entity_id: int, alias: str, *, lang: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO aliases (entity_type, entity_id, alias, normalized, lang) "
        "VALUES (?, ?, ?, ?, ?)",
        (entity_type, entity_id, alias, normalize_text(alias), lang),
    )


def add_retail_price_reference(
    conn: sqlite3.Connection, bottle_id: int, price_jpy: int, *, tax_included: bool,
    source_id: int | None, price_type: str = "msrp", effective_from: str | None = None,
    effective_to: str | None = None,
) -> int:
    """メーカー希望小売価格を登録する。出典 (source_id) がない定価は登録できない。

    出典は公式サイト・プレスリリース等、メーカーが公表したものに限る。
    API の販売価格 (kind='api') を定価として登録することも拒否する。
    """
    if source_id is None:
        raise ValueError("MSRP requires a source; unsourced list prices are not stored")
    row = conn.execute("SELECT kind FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown source {source_id}")
    if row["kind"] in ("api", "retailer_page"):
        raise ValueError("marketplace/retailer prices are not MSRP; use record_price_observation")
    return _insert(conn, "retail_price_references", {
        "bottle_id": bottle_id, "price_jpy": price_jpy, "tax_included": int(tax_included),
        "price_type": price_type, "effective_from": effective_from, "effective_to": effective_to,
        "source_id": source_id, "created_at": utcnow(),
    })


# ===== 販売側 =====


def upsert_shop(
    conn: sqlite3.Connection, marketplace: Marketplace, external_shop_id: str, name: str,
    url: str | None = None,
) -> int:
    conn.execute(
        """
        INSERT INTO shops (marketplace, external_shop_id, name, url, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (marketplace, external_shop_id) DO UPDATE SET
            name = excluded.name, url = COALESCE(excluded.url, shops.url)
        """,
        (marketplace, external_shop_id, name, url, utcnow()),
    )
    row = conn.execute(
        "SELECT id FROM shops WHERE marketplace = ? AND external_shop_id = ?",
        (marketplace, external_shop_id),
    ).fetchone()
    return int(row["id"])


@dataclass(frozen=True)
class ListingData:
    marketplace: Marketplace
    external_item_id: str
    title: str
    item_url: str | None = None
    jan_raw: str | None = None
    shop_id: int | None = None
    # API が構造化データとして返した仕様。None ならタイトルからパースする
    volume_ml: int | None = None
    abv_bp: int | None = None
    age_years: int | None = None
    batch_code: str | None = None


def upsert_listing(conn: sqlite3.Connection, data: ListingData) -> int:
    """出品を登録/更新。タイトルや JAN が変わった場合は照合をやり直すため
    match_status を 'unprocessed' に戻す (手動確定済みでも、内容が変われば再確認が必要)。"""
    parsed = parse_title(data.title)
    batch = data.batch_code or parsed.batch_code
    values = {
        "title": data.title,
        "item_url": data.item_url,
        "jan_raw": data.jan_raw,
        "shop_id": data.shop_id,
        "parsed_volume_ml": data.volume_ml if data.volume_ml is not None else parsed.volume_ml,
        "parsed_abv_bp": data.abv_bp if data.abv_bp is not None else parsed.abv_bp,
        "parsed_age_years": data.age_years if data.age_years is not None else parsed.age_years,
        "parsed_batch_code": normalize_batch_code(batch) if batch else None,
    }
    now = utcnow()
    existing = conn.execute(
        "SELECT id, title, jan_raw FROM listings WHERE marketplace = ? AND external_item_id = ?",
        (data.marketplace, data.external_item_id),
    ).fetchone()
    if existing is None:
        return _insert(conn, "listings", {
            "marketplace": data.marketplace, "external_item_id": data.external_item_id,
            **values, "first_seen_at": now, "last_seen_at": now,
        })
    changed = existing["title"] != data.title or existing["jan_raw"] != data.jan_raw
    sets = ", ".join(f"{k} = ?" for k in values)
    params: list[object] = [*values.values(), now]
    extra = ""
    if changed:
        extra = (", bottle_id = NULL, match_status = 'unprocessed', match_method = NULL, "
                 "match_score = NULL")
    conn.execute(
        f"UPDATE listings SET {sets}, last_seen_at = ?{extra} WHERE id = ?",
        (*params, existing["id"]),
    )
    return int(existing["id"])


@dataclass(frozen=True)
class PriceObservation:
    listing_id: int
    price_jpy: int
    source_id: int
    observed_at: str | None = None
    price_tax_included: bool | None = None
    tax_included_price_jpy: int | None = None
    shipping_fee_jpy: int | None = None
    shipping_condition: ShippingCondition = "unknown"
    in_stock: bool | None = None
    raw_response_id: int | None = None


def record_price_observation(conn: sqlite3.Connection, obs: PriceObservation) -> int:
    """実勢価格の観測を 1 件追記する。

    税込価格は「表示価格が税込」と分かる場合のみ自動で埋める。税抜表示の場合は
    API 等から税込額が明示されたときだけ保存し、推計値は作らない (端数処理が店舗ごとに異なるため)。
    """
    tax_incl = obs.tax_included_price_jpy
    if tax_incl is None and obs.price_tax_included:
        tax_incl = obs.price_jpy
    if obs.shipping_condition in ("included", "free") and obs.shipping_fee_jpy not in (None, 0):
        raise ValueError("shipping fee must be 0/None when shipping is free or included")
    shipping_fee = obs.shipping_fee_jpy
    if shipping_fee is None and obs.shipping_condition in ("included", "free"):
        shipping_fee = 0
    return _insert(conn, "price_observations", {
        "listing_id": obs.listing_id,
        "observed_at": obs.observed_at or utcnow(),
        "price_jpy": obs.price_jpy,
        "price_tax_included": None if obs.price_tax_included is None else int(obs.price_tax_included),
        "tax_included_price_jpy": tax_incl,
        "shipping_fee_jpy": shipping_fee,
        "shipping_condition": obs.shipping_condition,
        "in_stock": None if obs.in_stock is None else int(obs.in_stock),
        "source_id": obs.source_id,
        "raw_response_id": obs.raw_response_id,
        "created_at": utcnow(),
    })


def latest_prices_for_bottle(conn: sqlite3.Connection, bottle_id: int) -> list[sqlite3.Row]:
    """照合確定済み (matched) の出品について最新の観測を返す。要確認の出品は含めない。"""
    return conn.execute(
        """
        SELECT l.id AS listing_id, l.marketplace, l.title, l.item_url, s.name AS shop_name,
               p.observed_at, p.price_jpy, p.tax_included_price_jpy, p.shipping_fee_jpy,
               p.shipping_condition, p.in_stock
        FROM listings l
        JOIN price_observations p ON p.listing_id = l.id
        LEFT JOIN shops s ON s.id = l.shop_id
        WHERE l.bottle_id = ? AND l.match_status = 'matched'
          AND p.observed_at = (SELECT MAX(observed_at) FROM price_observations
                               WHERE listing_id = l.id)
        ORDER BY p.tax_included_price_jpy IS NULL, p.tax_included_price_jpy
        """,
        (bottle_id,),
    ).fetchall()


# ===== API 生レスポンス・情報源 =====


def get_or_create_source(
    conn: sqlite3.Connection, kind: SourceKind, title: str, *, url: str | None = None,
    publisher: str | None = None,
) -> int:
    """(kind, title) が同じ出典は使い回す (API ごとに 1 行)。"""
    row = conn.execute("SELECT id FROM sources WHERE kind = ? AND title = ?", (kind, title)).fetchone()
    if row is not None:
        return int(row["id"])
    return add_source(conn, kind, title, url=url, publisher=publisher)


def add_raw_response(
    conn: sqlite3.Connection, marketplace: str, endpoint: str, request_params: dict[str, object],
    status_code: int, body: str | None, *, fetched_at: str, expires_at: str | None,
) -> int:
    """API の生レスポンスを保存する。``request_params`` に認証情報を含めないこと
    (呼び出し側で除去済みであることを ``SECRET_PARAM_NAMES`` で再確認する)。"""
    leaked = SECRET_PARAM_NAMES & set(request_params)
    if leaked:
        raise ValueError(f"refusing to store secret params: {sorted(leaked)}")
    return _insert(conn, "api_raw_responses", {
        "marketplace": marketplace, "endpoint": endpoint,
        "request_params": json.dumps(request_params, ensure_ascii=False, sort_keys=True),
        "status_code": status_code, "body": body, "fetched_at": fetched_at, "expires_at": expires_at,
    })


def purge_expired_raw_responses(conn: sqlite3.Connection, now: str | None = None) -> int:
    """保持期限切れの生レスポンスを削除する。価格観測からの参照は NULL にして観測自体は残す。"""
    now = now or utcnow()
    with conn:
        conn.execute(
            "UPDATE price_observations SET raw_response_id = NULL WHERE raw_response_id IN "
            "(SELECT id FROM api_raw_responses WHERE expires_at IS NOT NULL AND expires_at <= ?)",
            (now,),
        )
        cur = conn.execute(
            "DELETE FROM api_raw_responses WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
        )
    return cur.rowcount
