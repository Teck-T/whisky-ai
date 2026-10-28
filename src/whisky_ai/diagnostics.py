"""実 API への接続チェック (``whisky-ai check-api``)。

自宅の Mac で初めて実キーを使うとき、失敗の原因 (キー・IP 制限・Referer) を切り分けるためのもの。
検索を 1 回だけ行い、DB には何も保存しない。
"""

from __future__ import annotations

import os
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from whisky_ai.collectors.base import Collector, SearchQuery
from whisky_ai.collectors.factory import Source, build_collector
from whisky_ai.collectors.rakuten import CollectorError
from whisky_ai.collectors.rate_limit import RateLimitExceeded

PUBLIC_IP_URL = "https://api.ipify.org"
TEST_KEYWORD = "ウイスキー"

REQUIRED_ENV: dict[Source, tuple[str, ...]] = {
    "rakuten": ("RAKUTEN_APPLICATION_ID", "RAKUTEN_ACCESS_KEY"),
    "yahoo": ("YAHOO_CLIENT_ID",),
}
OPTIONAL_ENV: dict[Source, tuple[str, ...]] = {
    "rakuten": ("RAKUTEN_REFERER", "RAKUTEN_AFFILIATE_ID"),
    "yahoo": (),
}


def mask(value: str) -> str:
    """キーの先頭 4 文字だけ見せる (どのキーを設定したかの確認用)。"""
    return "(未設定)" if not value else f"{value[:4]}…({len(value)}文字)"


def fetch_public_ipv4(timeout: float = 5.0) -> str:
    """このマシンのグローバル IPv4。楽天の「許可された IP アドレス」と照合するために表示する。"""
    with urllib.request.urlopen(PUBLIC_IP_URL, timeout=timeout) as resp:
        return str(resp.read().decode("ascii").strip())


@dataclass
class CheckResult:
    source: Source
    ok: bool
    lines: list[str] = field(default_factory=list)


def check_api(
    source: Source, *, env: Mapping[str, str] | None = None,
    public_ip: Callable[[], str] = fetch_public_ipv4,
    collector: Collector | None = None,
) -> CheckResult:
    env = os.environ if env is None else env
    res = CheckResult(source, ok=False)
    out = res.lines
    out.append(f"== {source} ==")
    missing = [k for k in REQUIRED_ENV[source] if not env.get(k)]
    for k in REQUIRED_ENV[source] + OPTIONAL_ENV[source]:
        v = env.get(k, "")
        out.append(f"  {k}: {v if k == 'RAKUTEN_REFERER' and v else mask(v)}")
    try:
        out.append(f"  このマシンのグローバル IPv4: {public_ip()}")
    except OSError as e:
        out.append(f"  グローバル IPv4: 取得失敗 ({e})")
    if missing:
        out.append(f"  NG: 必須の環境変数が未設定です: {', '.join(missing)}")
        out.append("      .env に記入し `set -a; source .env; set +a` で読み込んでください")
        return res

    collector = collector or build_collector(source, env=env)
    try:
        result = collector.search(SearchQuery(keyword=TEST_KEYWORD))
    except CollectorError as e:
        out.append(f"  NG: {e}")
        out.extend("  " + h for h in hints(source, str(e)))
        return res
    except RateLimitExceeded as e:
        out.append(f"  NG: {e} (時間をおいて再実行してください)")
        return res
    except OSError as e:
        out.append(f"  NG: 通信エラー: {e}")
        return res
    res.ok = True
    out.append(f"  OK: 「{TEST_KEYWORD}」の検索で {result.total_hits} 件中 {len(result.offers)} 件を取得")
    if result.offers:
        o = result.offers[0]
        out.append(f"      例: {o.title[:60]} / {o.price_jpy:,} 円 / {o.shop_name or ''}")
    return res


def hints(source: Source, message: str) -> list[str]:
    """エラーから考えられる原因。HTTP ステータスはメッセージ中の 'HTTP nnn' から読む。"""
    out: list[str] = []
    if "HTTP 403" in message and source == "rakuten":
        out += ["考えられる原因:",
                "  - 楽天アプリ設定の「許可された IP アドレス」に上のグローバル IPv4 が入っていない",
                "    (自宅回線の IP が変わった場合も同じエラーになります)",
                "  - RAKUTEN_REFERER がアプリ設定の URL と一致していない"]
    elif "HTTP 401" in message or "HTTP 400" in message:
        out += ["考えられる原因:",
                "  - ID / キーの値が間違っている (前後の空白やコピー漏れも確認)",
                "  - 楽天は applicationId と accessKey が同じアプリのものか確認"]
    elif "HTTP 429" in message:
        out += ["アクセス回数の上限です。時間をおいて再実行してください"]
    return out
