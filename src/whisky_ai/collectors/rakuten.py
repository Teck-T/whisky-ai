"""楽天市場商品検索 API クライアント (version 2026-07-01)。

仕様: https://webservice.rakuten.co.jp/documentation/ichiba-item-search
調査メモ: docs/api_research.md §1

* ``applicationId`` と ``accessKey`` が必須。``affiliateId`` は任意。
* レスポンスに JAN と送料額は含まれない (名前 + 仕様で照合する)。
* ``taxFlag`` 0=税込 / 1=税抜、``postageFlag`` 0=送料込 / 1=送料別、``availability`` 0/1。
"""

from __future__ import annotations

from typing import Any

from whisky_ai.collectors.base import Offer, RawResponse, SearchQuery, SearchResult
from whisky_ai.collectors.http import HttpResponse, Transport, UrllibTransport
from whisky_ai.collectors.rate_limit import RateLimiter, RetryPolicy, request_with_retry
from whisky_ai.db import utcnow
from whisky_ai.repository import SECRET_PARAM_NAMES, Marketplace, ShippingCondition

API_VERSION = "20260701"
ENDPOINT = f"https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/{API_VERSION}"
DOC_URL = "https://webservice.rakuten.co.jp/documentation/ichiba-item-search"
HITS_PER_PAGE = 30

_POSTAGE: dict[int, ShippingCondition] = {0: "included", 1: "separate"}


class CollectorError(RuntimeError):
    pass


class RakutenCollector:
    source_title = f"楽天市場商品検索API ({API_VERSION})"
    source_url = DOC_URL

    def __init__(
        self,
        application_id: str,
        access_key: str,
        *,
        referer: str | None = None,
        affiliate_id: str | None = None,
        transport: Transport | None = None,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        marketplace: Marketplace = "rakuten",
        is_mock: bool = False,
    ) -> None:
        if not application_id or not access_key:
            raise ValueError("applicationId and accessKey are required")
        self._application_id = application_id
        self._access_key = access_key
        self._referer = referer
        self._affiliate_id = affiliate_id
        self.transport = transport or UrllibTransport()
        self.limiter = limiter or RateLimiter()
        self.retry = retry or RetryPolicy()
        self.marketplace: Marketplace = marketplace
        self.is_mock = is_mock

    def _params(self, query: SearchQuery) -> dict[str, str]:
        if query.keyword is None:
            # 楽天の商品検索 API には JAN 検索パラメータがない
            raise ValueError("Rakuten item search requires a keyword (JAN search is not supported)")
        return {
            "keyword": query.keyword,
            "page": str(query.page),
            "hits": str(HITS_PER_PAGE),
            "format": "json",
            "formatVersion": "2",
        }

    def search(self, query: SearchQuery) -> SearchResult:
        public = self._params(query)
        secret = {"applicationId": self._application_id, "accessKey": self._access_key}
        if self._affiliate_id:
            secret["affiliateId"] = self._affiliate_id
        headers = {"Referer": self._referer} if self._referer else {}
        resp: HttpResponse = request_with_retry(
            lambda: self.transport.get(ENDPOINT, {**public, **secret}, headers),
            self.limiter, self.retry,
        )
        raw = RawResponse(ENDPOINT, dict(public), resp.status, resp.body, utcnow())
        assert not SECRET_PARAM_NAMES & set(raw.params)
        if resp.status != 200:
            raise CollectorError(f"Rakuten API HTTP {resp.status}: {_error_text(resp)}")
        data = resp.json()
        offers, warnings = [], []
        for item in _iter_items(data):
            try:
                offers.append(parse_item(item))
            except (KeyError, TypeError, ValueError) as e:
                warnings.append(f"skipped item {item.get('itemCode')!r}: {e}")
        return SearchResult(offers, raw, data.get("count"), warnings)


def _error_text(resp: HttpResponse) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.body[:200]
    # 新 API: {"errors": {"errorCode": 400, "errorMessage": "..."}}
    # 旧形式: {"error": "...", "error_description": "..."}
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, dict) and errors.get("errorMessage"):
        return str(errors["errorMessage"])[:200]
    if isinstance(data, dict):
        return str(data.get("error_description") or data.get("error") or data)[:200]
    return str(data)[:200]


def _iter_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    """formatVersion=2 (配列直下) と 1 ({"Item": {...}} で包む) の両方を受け付ける。"""
    items = data.get("Items") or data.get("items") or []
    return [it["Item"] if isinstance(it, dict) and "Item" in it else it for it in items]


def parse_item(item: dict[str, Any]) -> Offer:
    tax_flag = item.get("taxFlag")
    postage_flag = item.get("postageFlag")
    availability = item.get("availability")
    shop_code = item.get("shopCode") or str(item["itemCode"]).split(":", 1)[0]
    return Offer(
        external_item_id=str(item["itemCode"]),
        title=str(item["itemName"]),
        price_jpy=int(item["itemPrice"]),
        item_url=item.get("itemUrl"),
        price_tax_included={0: True, 1: False}.get(tax_flag) if tax_flag is not None else None,
        shipping_condition=_POSTAGE.get(postage_flag, "unknown") if postage_flag is not None else "unknown",
        shipping_fee_jpy=None,  # API は送料額を返さない
        in_stock={0: False, 1: True}.get(availability) if availability is not None else None,
        shop_external_id=shop_code,
        shop_name=item.get("shopName"),
        shop_url=item.get("shopUrl"),
        jan_raw=None,
    )
