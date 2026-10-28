"""価格収集モジュールのテスト。実通信はせず、FakeTransport / フィクスチャで検証する。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from whisky_ai import repository as repo
from whisky_ai.collectors.base import SearchQuery
from whisky_ai.collectors.factory import build_collector
from whisky_ai.collectors.http import FakeTransport, HttpResponse
from whisky_ai.collectors.mock import load_fixture
from whisky_ai.collectors.pipeline import collect
from whisky_ai.collectors.rakuten import ENDPOINT as RAKUTEN_ENDPOINT
from whisky_ai.collectors.rakuten import CollectorError, RakutenCollector, parse_item
from whisky_ai.collectors.rate_limit import RateLimiter, RateLimitExceeded, RetryPolicy
from whisky_ai.collectors.yahoo import YahooCollector, parse_hit
from whisky_ai.sample_data import SampleCatalog


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def _ok(body: object) -> HttpResponse:
    return HttpResponse(200, json.dumps(body, ensure_ascii=False))


# ---------- レート制限・再試行 ----------

def test_rate_limiter_enforces_min_interval() -> None:
    fc = FakeClock()
    lim = RateLimiter(1.0, clock=fc.clock, sleep=fc.sleep)
    lim.wait()
    fc.now += 0.3
    lim.wait()
    fc.now += 2.0
    lim.wait()
    assert fc.sleeps == [pytest.approx(0.7)]


def test_retry_on_429_with_backoff_then_success() -> None:
    fc = FakeClock()
    responses = iter([HttpResponse(429, "{}"), HttpResponse(429, "{}", {"Retry-After": "5"}),
                      _ok({"Items": []})])
    t = FakeTransport(lambda *_: next(responses))
    c = RakutenCollector("app", "key", transport=t, limiter=RateLimiter(1.0, fc.clock, fc.sleep))
    c.search(SearchQuery(keyword="x"))
    assert len(t.calls) == 3
    assert fc.sleeps == [2.0, 5.0]  # 指数バックオフ 2s → Retry-After 5s


def test_retry_gives_up() -> None:
    fc = FakeClock()
    t = FakeTransport(lambda *_: HttpResponse(429, "{}"))
    c = YahooCollector("cid", transport=t, limiter=RateLimiter(1.0, fc.clock, fc.sleep),
                       retry=RetryPolicy(max_retries=2))
    with pytest.raises(RateLimitExceeded):
        c.search(SearchQuery(keyword="x"))
    assert len(t.calls) == 3


def test_non_retryable_error_raises_without_leaking_key() -> None:
    body = {"error": "wrong_parameter", "error_description": "specify valid applicationId"}
    t = FakeTransport(lambda *_: HttpResponse(400, json.dumps(body)))
    c = RakutenCollector("SECRET-APP", "SECRET-KEY", transport=t, limiter=RateLimiter(0))
    with pytest.raises(CollectorError) as e:
        c.search(SearchQuery(keyword="x"))
    assert "SECRET" not in str(e.value) and "valid applicationId" in str(e.value)
    assert len(t.calls) == 1


def test_error_formats_from_live_endpoints() -> None:
    """2026-10-27 にキーなしで実エンドポイントへ 1 回ずつ送って得た実際のエラー形式。"""
    rk = {"errors": {"errorCode": 400,
                     "errorMessage": "applicationId must be present in the query parameter or request body"}}
    c = RakutenCollector("a", "k", transport=FakeTransport(lambda *_: HttpResponse(400, json.dumps(rk))),
                         limiter=RateLimiter(0))
    with pytest.raises(CollectorError, match="applicationId must be present"):
        c.search(SearchQuery(keyword="x"))
    yh = {"Error": {"Message": "Bad Request: Authentication parameters in your request incompleted."}}
    y = YahooCollector("c", transport=FakeTransport(lambda *_: HttpResponse(401, json.dumps(yh))),
                       limiter=RateLimiter(0))
    with pytest.raises(CollectorError, match="Authentication parameters"):
        y.search(SearchQuery(keyword="x"))


# ---------- 楽天 ----------

def test_rakuten_request_params_and_headers() -> None:
    t = FakeTransport(lambda *_: _ok({"Items": []}))
    c = RakutenCollector("app", "key", referer="https://example.invalid/", affiliate_id="aff",
                         transport=t, limiter=RateLimiter(0))
    res = c.search(SearchQuery(keyword="Aokawa 12", page=2))
    url, params, headers = t.calls[0]
    assert url == RAKUTEN_ENDPOINT and "openapi.rakuten.co.jp" in url
    assert params["applicationId"] == "app" and params["accessKey"] == "key"
    assert params["affiliateId"] == "aff" and params["page"] == "2"
    assert headers == {"Referer": "https://example.invalid/"}
    assert not {"applicationId", "accessKey", "affiliateId"} & set(res.raw.params)


def test_rakuten_rejects_jan_only_query() -> None:
    c = RakutenCollector("app", "key", transport=FakeTransport(lambda *_: _ok({})))
    with pytest.raises(ValueError, match="JAN"):
        c.search(SearchQuery(jan="2900000000049"))


@pytest.mark.parametrize(("tax", "postage", "avail", "exp"), [
    (0, 0, 1, (True, "included", True)),
    (1, 1, 0, (False, "separate", False)),
    (None, None, None, (None, "unknown", None)),
])
def test_rakuten_flag_mapping(tax: int | None, postage: int | None, avail: int | None,
                              exp: tuple[object, ...]) -> None:
    o = parse_item({"itemCode": "s:1", "itemName": "n", "itemPrice": 100, "taxFlag": tax,
                    "postageFlag": postage, "availability": avail})
    assert (o.price_tax_included, o.shipping_condition, o.in_stock) == exp
    assert o.shipping_fee_jpy is None and o.jan_raw is None and o.shop_external_id == "s"


def test_rakuten_accepts_format_version_1() -> None:
    body = {"count": 1, "Items": [{"Item": {"itemCode": "s:1", "itemName": "n", "itemPrice": 1}}]}
    c = RakutenCollector("a", "k", transport=FakeTransport(lambda *_: _ok(body)), limiter=RateLimiter(0))
    assert [o.external_item_id for o in c.search(SearchQuery(keyword="n")).offers] == ["s:1"]


def test_rakuten_skips_malformed_item_with_warning() -> None:
    body = {"Items": [{"itemCode": "s:1", "itemName": "n"}, {"itemCode": "s:2", "itemName": "m",
                                                             "itemPrice": 5}]}
    c = RakutenCollector("a", "k", transport=FakeTransport(lambda *_: _ok(body)), limiter=RateLimiter(0))
    res = c.search(SearchQuery(keyword="n"))
    assert [o.external_item_id for o in res.offers] == ["s:2"]
    assert len(res.warnings) == 1


# ---------- Yahoo! ----------

def test_yahoo_request_params_and_paging_limit() -> None:
    t = FakeTransport(lambda *_: _ok({"hits": []}))
    c = YahooCollector("cid", transport=t, limiter=RateLimiter(0))
    res = c.search(SearchQuery(keyword="Aokawa", jan="2900000000049", page=3))
    _, params, _ = t.calls[0]
    assert params == {"appid": "cid", "query": "Aokawa", "jan_code": "2900000000049",
                      "results": "30", "start": "61"}
    assert "appid" not in res.raw.params
    with pytest.raises(ValueError, match="1000"):
        c.search(SearchQuery(keyword="x", page=34))


def test_yahoo_hit_mapping() -> None:
    base = {"code": "s_1", "name": "n", "price": 1100, "url": "u",
            "seller": {"sellerId": "s", "name": "S"}}
    o = parse_hit({**base, "taxExcludePrice": 1000, "inStock": True, "janCode": "2900000000049",
                   "shipping": {"code": 2}})
    assert (o.price_tax_included, o.tax_included_price_jpy) == (True, 1100)
    assert (o.shipping_condition, o.in_stock, o.jan_raw, o.shop_external_id) == (
        "free", True, "2900000000049", "s")
    o2 = parse_hit({**base, "shipping": {"code": 3}, "janCode": ""})
    assert (o2.price_tax_included, o2.tax_included_price_jpy) == (None, None)  # 税込区分は推測しない
    assert o2.shipping_condition == "conditional_free" and o2.jan_raw is None and o2.in_stock is None
    assert parse_hit({**base, "shipping": {"code": 1}}).shipping_condition == "unknown"


# ---------- factory ----------

def test_factory_uses_mock_without_keys() -> None:
    for source in ("rakuten", "yahoo"):
        c = build_collector(source, env={})  # type: ignore[arg-type]
        assert c.is_mock and c.marketplace == "mock"


def test_factory_uses_real_client_with_keys() -> None:
    r = build_collector("rakuten", env={"RAKUTEN_APPLICATION_ID": "a", "RAKUTEN_ACCESS_KEY": "k"})
    y = build_collector("yahoo", env={"YAHOO_CLIENT_ID": "c"})
    assert (r.is_mock, r.marketplace, y.is_mock, y.marketplace) == (False, "rakuten", False, "yahoo")
    # キーの片方だけでは実通信しない
    assert build_collector("rakuten", env={"RAKUTEN_APPLICATION_ID": "a"}).is_mock


def test_fixtures_are_fictional() -> None:
    for name in ("rakuten_search.json", "yahoo_search.json"):
        data = load_fixture(name)
        text = json.dumps(data, ensure_ascii=False)
        assert "架空" in data["_comment"] and "https://" not in text.replace("https://example.invalid", "")


# ---------- pipeline ----------

def test_pipeline_with_mock_rakuten(conn: sqlite3.Connection, catalog: SampleCatalog) -> None:
    s = collect(conn, build_collector("rakuten", env={}), SearchQuery(keyword="700ml"))
    assert s.offers == s.observations == 6
    assert s.match_status == {"matched": 4, "pending_review": 1, "unmatched": 1}
    # 税抜表示 (taxFlag=1) の出品は税込価格を推計しない
    row = conn.execute(
        "SELECT p.* FROM price_observations p JOIN listings l ON l.id = p.listing_id "
        "WHERE l.external_item_id = 'mock-liquor:aw12'").fetchone()
    assert (row["price_jpy"], row["price_tax_included"], row["tax_included_price_jpy"]) == (5800, 0, None)
    assert (row["shipping_condition"], row["shipping_fee_jpy"]) == ("separate", None)
    src = conn.execute("SELECT kind FROM sources WHERE id = ?", (row["source_id"],)).fetchone()[0]
    assert src == "sample"  # mock データは API 由来と区別する


def test_pipeline_with_mock_yahoo_jan(conn: sqlite3.Connection, catalog: SampleCatalog) -> None:
    s = collect(conn, build_collector("yahoo", env={}), SearchQuery(keyword="700ml"))
    assert s.match_status == {"matched": 4, "pending_review": 1}
    rows = {r["external_item_id"]: r for r in conn.execute(
        "SELECT l.external_item_id, l.match_status, l.match_method, b.display_name FROM listings l "
        "LEFT JOIN bottles b ON b.id = l.bottle_id")}
    assert rows["mock-store_aw12"]["match_method"] == "jan"
    assert rows["mock-shop2_kcs"]["match_status"] == "pending_review"  # バッチ間で共有された JAN
    assert rows["mock-shop2_mp43"]["display_name"] == "Mistral Peak Original 700ml 43%"  # JAN で度数確定
    assert rows["mock-shop2_bad"]["match_method"] == "name_spec"  # 不正 JAN は無視


def test_pipeline_appends_history_and_keeps_manual_decision(
    conn: sqlite3.Connection, catalog: SampleCatalog
) -> None:
    from whisky_ai.matching import accept_candidate

    c = build_collector("rakuten", env={})
    collect(conn, c, SearchQuery(keyword="Mistral"))
    lid = conn.execute("SELECT id FROM listings WHERE external_item_id = 'mock-liquor:mp'").fetchone()[0]
    cid = conn.execute("SELECT id FROM match_candidates WHERE listing_id = ? ORDER BY id LIMIT 1",
                       (lid,)).fetchone()[0]
    accept_candidate(conn, cid, "tester")
    s = collect(conn, c, SearchQuery(keyword="Mistral"))
    assert s.match_status == {"matched": 1}
    assert conn.execute("SELECT COUNT(*) FROM price_observations WHERE listing_id = ?",
                        (lid,)).fetchone()[0] == 2
    assert conn.execute("SELECT match_method FROM listings WHERE id = ?", (lid,)).fetchone()[0] == "manual"


def test_raw_response_storage_and_purge(conn: sqlite3.Connection, catalog: SampleCatalog) -> None:
    collect(conn, build_collector("yahoo", env={}), SearchQuery(keyword="Aokawa"), keep_raw_days=7)
    raw = conn.execute("SELECT * FROM api_raw_responses").fetchone()
    assert "appid" not in json.loads(raw["request_params"])
    assert raw["expires_at"] > raw["fetched_at"]
    assert repo.purge_expired_raw_responses(conn, now="2000-01-01T00:00:00Z") == 0
    assert repo.purge_expired_raw_responses(conn, now="2999-01-01T00:00:00Z") == 1
    obs = conn.execute("SELECT raw_response_id FROM price_observations").fetchall()
    assert obs and all(r[0] is None for r in obs)  # 観測は残し、参照だけ外す


def test_raw_response_not_stored_when_retention_zero(
    conn: sqlite3.Connection, catalog: SampleCatalog
) -> None:
    collect(conn, build_collector("yahoo", env={}), SearchQuery(keyword="Aokawa"), keep_raw_days=0)
    assert conn.execute("SELECT COUNT(*) FROM api_raw_responses").fetchone()[0] == 0


def test_add_raw_response_refuses_secrets(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="secret"):
        repo.add_raw_response(conn, "rakuten", "e", {"accessKey": "x"}, 200, "{}",
                              fetched_at="2026-01-01T00:00:00Z", expires_at=None)


def test_real_transport_not_used_in_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    def boom(*_: object, **__: object) -> None:
        raise AssertionError("network access in mock mode")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    c = build_collector("yahoo", env={})
    assert c.search(SearchQuery(keyword="Aokawa")).offers
