"""Yahoo!ショッピング 商品検索 API v3 クライアント。

仕様: https://developer.yahoo.co.jp/webapi/shopping/v3/itemsearch.html
調査メモ: docs/api_research.md §2

* ``appid`` (Client ID) が必須。1 クエリ/秒。``start + results`` は 1,000 まで。
* ``jan_code`` で JAN 検索でき、レスポンスに ``janCode`` がある。
* 送料は ``shipping.code`` (1=設定なし / 2=送料無料 / 3=条件付き送料無料) のみで、金額は返らない。
"""

from __future__ import annotations

from typing import Any

from whisky_ai.collectors.base import Offer, RawResponse, SearchQuery, SearchResult
from whisky_ai.collectors.http import HttpResponse, Transport, UrllibTransport
from whisky_ai.collectors.rakuten import CollectorError
from whisky_ai.collectors.rate_limit import RateLimiter, RetryPolicy, request_with_retry
from whisky_ai.db import utcnow
from whisky_ai.repository import SECRET_PARAM_NAMES, Marketplace, ShippingCondition

ENDPOINT = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
DOC_URL = "https://developer.yahoo.co.jp/webapi/shopping/v3/itemsearch.html"
RESULTS_PER_PAGE = 30
MAX_START_PLUS_RESULTS = 1000

_SHIPPING: dict[int, ShippingCondition] = {1: "unknown", 2: "free", 3: "conditional_free"}


class YahooCollector:
    source_title = "Yahoo!ショッピング商品検索API (v3)"
    source_url = DOC_URL

    def __init__(
        self,
        client_id: str,
        *,
        transport: Transport | None = None,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        marketplace: Marketplace = "yahoo",
        is_mock: bool = False,
    ) -> None:
        if not client_id:
            raise ValueError("client_id (appid) is required")
        self._client_id = client_id
        self.transport = transport or UrllibTransport()
        self.limiter = limiter or RateLimiter()
        self.retry = retry or RetryPolicy()
        self.marketplace: Marketplace = marketplace
        self.is_mock = is_mock

    def _params(self, query: SearchQuery) -> dict[str, str]:
        start = (query.page - 1) * RESULTS_PER_PAGE + 1
        if start + RESULTS_PER_PAGE > MAX_START_PLUS_RESULTS:
            raise ValueError(f"page {query.page} exceeds the API limit (start + results <= 1000)")
        params = {"results": str(RESULTS_PER_PAGE), "start": str(start)}
        if query.keyword:
            params["query"] = query.keyword
        if query.jan:
            params["jan_code"] = query.jan
        return params

    def search(self, query: SearchQuery) -> SearchResult:
        public = self._params(query)
        resp: HttpResponse = request_with_retry(
            lambda: self.transport.get(ENDPOINT, {**public, "appid": self._client_id}, {}),
            self.limiter, self.retry,
        )
        raw = RawResponse(ENDPOINT, dict(public), resp.status, resp.body, utcnow())
        assert not SECRET_PARAM_NAMES & set(raw.params)
        if resp.status != 200:
            raise CollectorError(f"Yahoo! API HTTP {resp.status}: {_error_text(resp)}")
        data = resp.json()
        offers, warnings = [], []
        for hit in data.get("hits", []):
            try:
                offers.append(parse_hit(hit))
            except (KeyError, TypeError, ValueError) as e:
                warnings.append(f"skipped hit {hit.get('code')!r}: {e}")
        return SearchResult(offers, raw, data.get("totalResultsAvailable"), warnings)


def parse_hit(hit: dict[str, Any]) -> Offer:
    price = int(hit["price"])
    excl = hit.get("taxExcludePrice")
    # 税込区分は推測しない: 税抜価格が併記され、表示価格より小さい場合だけ「税込」と判断する
    tax_included: bool | None = None
    if isinstance(excl, int | float) and not isinstance(excl, bool) and 0 < excl < price:
        tax_included = True
    shipping = hit.get("shipping") or {}
    seller = hit.get("seller") or {}
    in_stock = hit.get("inStock")
    return Offer(
        external_item_id=str(hit["code"]),
        title=str(hit["name"]),
        price_jpy=price,
        item_url=hit.get("url"),
        price_tax_included=tax_included,
        tax_included_price_jpy=price if tax_included else None,
        shipping_condition=_SHIPPING.get(_int_or_none(shipping.get("code")) or 1, "unknown"),
        shipping_fee_jpy=None,  # API は送料額を返さない
        in_stock=in_stock if isinstance(in_stock, bool) else None,
        shop_external_id=seller.get("sellerId"),
        shop_name=seller.get("name"),
        shop_url=seller.get("url"),
        jan_raw=hit.get("janCode") or None,
    )


def _error_text(resp: HttpResponse) -> str:
    """エラー形式: {"Error": {"Message": "..."}}"""
    try:
        data = resp.json()
    except ValueError:
        return resp.body[:200]
    err = data.get("Error") if isinstance(data, dict) else None
    if isinstance(err, dict) and err.get("Message"):
        return str(err["Message"])[:200]
    return resp.body[:200]


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
