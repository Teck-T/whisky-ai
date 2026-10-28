"""APIキーなしで動かすためのフィクスチャ Transport。

フィクスチャ (``fixtures/*.json``) は各 API のレスポンス形式を模した架空データで、
実際のパーサ (``rakuten.parse_item`` / ``yahoo.parse_hit``) をそのまま通る。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from typing import Any

from whisky_ai.collectors.http import HttpResponse
from whisky_ai.normalize import normalize_text


def load_fixture(name: str) -> dict[str, Any]:
    text = (resources.files("whisky_ai.collectors") / "fixtures" / name).read_text("utf-8")
    data: dict[str, Any] = json.loads(text)
    return data


def _matches(name: str, keyword: str | None) -> bool:
    if not keyword:
        return True
    hay = normalize_text(name).replace(" ", "")
    return all(tok in hay for tok in normalize_text(keyword).split())


class FixtureTransport:
    """キーワード (全トークンを含む) と JAN で絞り込んだフィクスチャを返す。"""

    def __init__(self, fixture: str) -> None:
        self.data = load_fixture(fixture)
        self.calls = 0

    def get(self, url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> HttpResponse:
        self.calls += 1
        keyword = params.get("keyword") or params.get("query")
        jan = params.get("jan_code")
        data = dict(self.data)
        if "Items" in data:
            data["Items"] = [i for i in data["Items"] if _matches(i["itemName"], keyword)]
            data["count"] = len(data["Items"])
        else:
            data["hits"] = [h for h in data["hits"]
                            if _matches(h["name"], keyword) and (not jan or h.get("janCode") == jan)]
            data["totalResultsAvailable"] = len(data["hits"])
        return HttpResponse(200, json.dumps(data, ensure_ascii=False))
