"""レート制限と再試行。

両 API とも短時間の集中アクセスで一時的に利用停止になりうる。既定は 1 リクエスト/秒
(Yahoo! は公式に 1 クエリ/秒。楽天は数値非公開のため同値で保守的に運用)。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from whisky_ai.collectors.http import HttpResponse

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RateLimitExceeded(RuntimeError):
    """再試行しても 429/5xx が続いた。"""


@dataclass
class RateLimiter:
    min_interval: float = 1.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last: float | None = field(default=None, init=False)

    def wait(self) -> None:
        now = self.clock()
        if self._last is not None:
            delay = self._last + self.min_interval - now
            if delay > 0:
                self.sleep(delay)
                now = self.clock()
        self._last = now


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 2.0
    max_delay: float = 60.0

    def delay_for(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return min(self.max_delay, max(0.0, float(retry_after)))
            except ValueError:
                pass  # HTTP-date 形式は扱わず指数バックオフにする
        return min(self.max_delay, self.base_delay * 2.0**attempt)


def request_with_retry(
    send: Callable[[], HttpResponse], limiter: RateLimiter, policy: RetryPolicy,
) -> HttpResponse:
    """リクエストごとにレート制限を守り、429/5xx は指数バックオフで再試行する。"""
    for attempt in range(policy.max_retries + 1):
        limiter.wait()
        resp = send()
        if resp.status not in RETRYABLE_STATUS:
            return resp
        if attempt == policy.max_retries:
            break
        retry_after = {k.lower(): v for k, v in resp.headers.items()}.get("retry-after")
        limiter.sleep(policy.delay_for(attempt, retry_after))
    raise RateLimitExceeded(f"HTTP {resp.status} after {policy.max_retries} retries")
