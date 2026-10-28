"""Collector の共通型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from whisky_ai.repository import Marketplace, ShippingCondition


@dataclass(frozen=True)
class SearchQuery:
    keyword: str | None = None
    jan: str | None = None
    page: int = 1

    def __post_init__(self) -> None:
        if not (self.keyword or self.jan):
            raise ValueError("keyword or jan is required")
        if self.page < 1:
            raise ValueError("page must be >= 1")


@dataclass(frozen=True)
class Offer:
    """API の 1 商品を、保存に必要な項目だけに正規化したもの。

    不明な値は推測で埋めず None にする (税込区分・送料額・在庫など)。
    """

    external_item_id: str
    title: str
    price_jpy: int
    item_url: str | None = None
    price_tax_included: bool | None = None
    tax_included_price_jpy: int | None = None
    shipping_condition: ShippingCondition = "unknown"
    shipping_fee_jpy: int | None = None
    in_stock: bool | None = None
    shop_external_id: str | None = None
    shop_name: str | None = None
    shop_url: str | None = None
    jan_raw: str | None = None


@dataclass(frozen=True)
class RawResponse:
    endpoint: str
    params: dict[str, object]  # 認証情報は除去済み
    status_code: int
    body: str
    fetched_at: str


@dataclass(frozen=True)
class SearchResult:
    offers: list[Offer]
    raw: RawResponse
    total_hits: int | None = None
    warnings: list[str] = field(default_factory=list)


class Collector(Protocol):
    marketplace: Marketplace
    #: True ならフィクスチャ由来 (APIキーなし)。保存時の出典を 'sample' にする
    is_mock: bool
    source_title: str
    source_url: str

    def search(self, query: SearchQuery) -> SearchResult: ...
