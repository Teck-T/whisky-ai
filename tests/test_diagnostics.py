from __future__ import annotations

import json

from whisky_ai.collectors.http import FakeTransport, HttpResponse
from whisky_ai.collectors.rakuten import RakutenCollector
from whisky_ai.collectors.rate_limit import RateLimiter
from whisky_ai.collectors.yahoo import YahooCollector
from whisky_ai.diagnostics import check_api, mask

ENV = {"RAKUTEN_APPLICATION_ID": "app-1234567", "RAKUTEN_ACCESS_KEY": "key-abcdefg",
       "RAKUTEN_REFERER": "https://github.com/Teck-T/whisky-ai", "YAHOO_CLIENT_ID": "cid-xyz"}


def _rakuten(status: int, body: object) -> RakutenCollector:
    t = FakeTransport(lambda *_: HttpResponse(status, json.dumps(body, ensure_ascii=False)))
    return RakutenCollector("a", "k", transport=t, limiter=RateLimiter(0))


def test_mask_hides_keys() -> None:
    assert mask("") == "(未設定)"
    assert mask("abcdefghij") == "abcd…(10文字)"


def test_missing_env_reports_without_request() -> None:
    res = check_api("rakuten", env={}, public_ip=lambda: "203.0.113.45")
    text = "\n".join(res.lines)
    assert not res.ok
    assert "203.0.113.45" in text and "RAKUTEN_APPLICATION_ID" in text and "未設定" in text


def test_ok_shows_sample_and_masks_keys() -> None:
    body = {"count": 1234, "Items": [{"itemCode": "s:1", "itemName": "テストウイスキー 700ml",
                                      "itemPrice": 3300, "shopName": "店"}]}
    res = check_api("rakuten", env=ENV, public_ip=lambda: "203.0.113.45",
                    collector=_rakuten(200, body))
    text = "\n".join(res.lines)
    assert res.ok and "1234 件" in text and "3,300 円" in text
    assert "key-abcdefg" not in text and "key-…" in text
    assert "https://github.com/Teck-T/whisky-ai" in text  # Referer は照合のため全文表示


def test_rakuten_403_hints_ip_and_referer() -> None:
    res = check_api("rakuten", env=ENV, public_ip=lambda: "203.0.113.45",
                    collector=_rakuten(403, {"errors": {"errorCode": 403, "errorMessage": "Forbidden"}}))
    text = "\n".join(res.lines)
    assert not res.ok and "許可された IP アドレス" in text and "RAKUTEN_REFERER" in text


def test_yahoo_401_hints_key() -> None:
    t = FakeTransport(lambda *_: HttpResponse(401, json.dumps({"Error": {"Message": "bad appid"}})))
    res = check_api("yahoo", env=ENV, public_ip=lambda: "203.0.113.45",
                    collector=YahooCollector("c", transport=t, limiter=RateLimiter(0)))
    assert not res.ok and any("ID / キー" in line for line in res.lines)


def test_public_ip_failure_is_not_fatal() -> None:
    def boom() -> str:
        raise OSError("offline")

    res = check_api("rakuten", env=ENV, public_ip=boom, collector=_rakuten(200, {"Items": []}))
    assert res.ok and any("取得失敗" in line for line in res.lines)
