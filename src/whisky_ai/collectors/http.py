"""最小限の HTTP 層 (標準ライブラリ urllib)。

テストでは ``Transport`` を差し替えて実通信しない。プロキシは urllib が
環境変数 HTTPS_PROXY を自動で使う。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from whisky_ai import __version__

USER_AGENT = f"whisky-ai/{__version__} (+personal price research)"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body)


class Transport(Protocol):
    def get(self, url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> HttpResponse: ...


class UrllibTransport:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def get(self, url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> HttpResponse:
        full = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return HttpResponse(resp.status, resp.read().decode("utf-8"), dict(resp.headers))
        except urllib.error.HTTPError as e:
            return HttpResponse(e.code, e.read().decode("utf-8", "replace"), dict(e.headers or {}))


Handler = Callable[[str, Mapping[str, str], Mapping[str, str]], HttpResponse]


class FakeTransport:
    """テスト用。呼び出しを記録し、handler の戻り値を返す。"""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def get(self, url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> HttpResponse:
        self.calls.append((url, dict(params), dict(headers)))
        return self.handler(url, params, headers)
