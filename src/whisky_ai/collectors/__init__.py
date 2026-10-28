"""マーケットプレイス価格収集。

各 Collector は検索結果を共通の ``Offer`` に変換して返すだけで、DB には触れない。
保存・照合は ``pipeline.collect`` が ``repository`` / ``matching`` を使って行う。
"""

from whisky_ai.collectors.base import Collector, Offer, SearchQuery, SearchResult

__all__ = ["Collector", "Offer", "SearchQuery", "SearchResult"]
