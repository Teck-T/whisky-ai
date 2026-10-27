"""環境変数から Collector を組み立てる。キーがなければフィクスチャ (mock) を使う。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from whisky_ai.collectors.base import Collector
from whisky_ai.collectors.mock import FixtureTransport
from whisky_ai.collectors.rakuten import RakutenCollector
from whisky_ai.collectors.rate_limit import RateLimiter
from whisky_ai.collectors.yahoo import YahooCollector

Source = Literal["rakuten", "yahoo"]


def build_collector(
    source: Source, *, env: Mapping[str, str] | None = None, force_mock: bool = False,
) -> Collector:
    """``force_mock`` か API キー未設定ならフィクスチャを返す Collector (marketplace='mock')。

    mock の場合は実通信しないのでレート制限の待ちも入れない。
    """
    env = os.environ if env is None else env
    if source == "rakuten":
        app_id = env.get("RAKUTEN_APPLICATION_ID", "")
        key = env.get("RAKUTEN_ACCESS_KEY", "")
        if force_mock or not (app_id and key):
            return RakutenCollector("mock", "mock", transport=FixtureTransport("rakuten_search.json"),
                                    limiter=RateLimiter(0), marketplace="mock", is_mock=True)
        return RakutenCollector(app_id, key, referer=env.get("RAKUTEN_REFERER") or None,
                                affiliate_id=env.get("RAKUTEN_AFFILIATE_ID") or None)
    if source == "yahoo":
        client_id = env.get("YAHOO_CLIENT_ID", "")
        if force_mock or not client_id:
            return YahooCollector("mock", transport=FixtureTransport("yahoo_search.json"),
                                  limiter=RateLimiter(0), marketplace="mock", is_mock=True)
        return YahooCollector(client_id)
    raise ValueError(f"unknown source: {source}")
