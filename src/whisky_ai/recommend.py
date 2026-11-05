"""香味プロファイルにもとづく推薦。

リリース単位で、妥当な抽出結果 (``flavor_extractions.is_valid=1``) の数値軸を平均したものを
プロファイルとする。抽出がボトル・バッチ・レビューに紐付いていても、リリースにたどって集計する。

類似度 = (1 - 共通軸の平均絶対差) × 被覆率 (0..1)。

* 欠けた軸を 0 とみなすと「その軸に言及しなかった」ことと「その特徴がない」ことが混ざるため、
  値の比較は共通軸だけで行う。
* ただし共通軸だけだと、片方にしかない特徴 (例: シェリー) を無視してしまうので、被覆率で割り引く。
  リリース同士の比較では |共通| / |和集合|、好みの指定 (taste) との比較では |共通| / |指定した軸|。
* 共通軸が少ない組は除外する。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from whisky_ai.flavor import AXES

MIN_COMMON_AXES = 3

# 抽出の紐付け先 (release / batch / bottle / review) からリリース ID を求める
_EXTRACTION_RELEASE_SQL = """
SELECT fe.id AS extraction_id,
       COALESCE(fe.release_id, bt.release_id, bo.release_id,
                rv.release_id, rbt.release_id, rbo.release_id) AS release_id
FROM flavor_extractions fe
LEFT JOIN batches bt ON bt.id = fe.batch_id
LEFT JOIN bottles bo ON bo.id = fe.bottle_id
LEFT JOIN reviews rv ON rv.id = fe.review_id
LEFT JOIN batches rbt ON rbt.id = rv.batch_id
LEFT JOIN bottles rbo ON rbo.id = rv.bottle_id
WHERE fe.is_valid = 1
"""


@dataclass
class Profile:
    release_id: int
    name: str
    axes: dict[str, float]
    n_extractions: int
    top_notes: list[str] = field(default_factory=list)


@dataclass
class Recommendation:
    profile: Profile
    similarity: float
    common_axes: int
    best_price_jpy: int | None
    best_price_url: str | None


def load_profiles(conn: sqlite3.Connection) -> dict[int, Profile]:
    sums: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    counts: dict[int, set[int]] = defaultdict(set)
    notes: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in conn.execute(
        f"SELECT x.release_id, x.extraction_id, a.axis, a.value FROM ({_EXTRACTION_RELEASE_SQL}) x"
        " JOIN flavor_axes a ON a.extraction_id = x.extraction_id WHERE x.release_id IS NOT NULL"
    ):
        sums[row["release_id"]][row["axis"]].append(row["value"])
        counts[row["release_id"]].add(row["extraction_id"])
    for row in conn.execute(
        f"SELECT x.release_id, n.descriptor FROM ({_EXTRACTION_RELEASE_SQL}) x"
        " JOIN flavor_notes n ON n.extraction_id = x.extraction_id WHERE x.release_id IS NOT NULL"
    ):
        notes[row["release_id"]][row["descriptor"]] += 1
    names = {r["id"]: f"{r['brand']} {r['name']}" for r in conn.execute(
        "SELECT r.id, r.name, b.name AS brand FROM releases r JOIN brands b ON b.id = r.brand_id")}
    out = {}
    for rid, axes in sums.items():
        top = sorted(notes[rid].items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        out[rid] = Profile(rid, names.get(rid, str(rid)),
                           {a: round(sum(v) / len(v), 4) for a, v in axes.items()},
                           len(counts[rid]), [d for d, _ in top])
    return out


def similarity(
    a: dict[str, float], b: dict[str, float], *, query: bool = False
) -> tuple[float, int]:
    """(類似度, 共通軸数)。``query=True`` は a を好みの指定とみなし、b の余分な軸で減点しない。"""
    common = a.keys() & b.keys()
    if not common:
        return 0.0, 0
    diff = sum(abs(a[k] - b[k]) for k in common) / len(common)
    coverage = len(common) / (len(a) if query else len(a.keys() | b.keys()))
    return round((1 - diff) * coverage, 4), len(common)


def parse_taste(spec: str) -> dict[str, float]:
    """'sweetness=0.8,smokiness=0.2' → dict。未知の軸や範囲外は ValueError。"""
    out: dict[str, float] = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        key, _, val = part.partition("=")
        key = key.strip()
        if key not in AXES:
            raise ValueError(f"unknown axis {key!r}; choose from {', '.join(sorted(AXES))}")
        v = float(val)
        if not 0 <= v <= 1:
            raise ValueError(f"{key} must be in [0, 1]")
        out[key] = v
    if not out:
        raise ValueError("taste is empty")
    return out


def best_prices(conn: sqlite3.Connection) -> dict[int, tuple[int, str | None]]:
    """リリースごとの最安の税込実勢価格 (照合確定済み・在庫ありか不明・各出品の最新観測)。

    送料は含めない (金額が API から得られないことが多いため、比較条件をそろえられない)。
    """
    rows = conn.execute(
        """
        SELECT b.release_id, p.tax_included_price_jpy AS price, l.item_url
        FROM listings l
        JOIN bottles b ON b.id = l.bottle_id
        JOIN price_observations p ON p.listing_id = l.id
        WHERE l.match_status = 'matched'
          AND p.tax_included_price_jpy IS NOT NULL
          AND COALESCE(p.in_stock, 1) = 1
          AND p.id = (SELECT p2.id FROM price_observations p2 WHERE p2.listing_id = l.id
                      ORDER BY p2.observed_at DESC, p2.id DESC LIMIT 1)
        """
    )
    best: dict[int, tuple[int, str | None]] = {}
    for r in rows:
        cur = best.get(r["release_id"])
        if cur is None or r["price"] < cur[0]:
            best[r["release_id"]] = (r["price"], r["item_url"])
    return best


def recommend(
    conn: sqlite3.Connection, *, like_release_id: int | None = None,
    taste: dict[str, float] | None = None, limit: int = 10, max_price_jpy: int | None = None,
    min_common_axes: int = MIN_COMMON_AXES,
) -> list[Recommendation]:
    """似たリリースを類似度順に返す。``like_release_id`` か ``taste`` のどちらかを指定する。

    ``max_price_jpy`` を指定すると、実勢価格がわかっていてその額以下のものだけを返す。
    """
    if (like_release_id is None) == (taste is None):
        raise ValueError("specify exactly one of like_release_id or taste")
    profiles = load_profiles(conn)
    if like_release_id is not None:
        if like_release_id not in profiles:
            raise KeyError(f"release {like_release_id} has no valid flavor extractions")
        target = profiles[like_release_id].axes
    else:
        assert taste is not None
        target = taste
    need = min(min_common_axes, len(target))
    prices = best_prices(conn)
    out = []
    for rid, prof in profiles.items():
        if rid == like_release_id:
            continue
        sim, n = similarity(target, prof.axes, query=taste is not None)
        if n < need:
            continue
        price = prices.get(rid)
        if max_price_jpy is not None and (price is None or price[0] > max_price_jpy):
            continue
        out.append(Recommendation(prof, sim, n, price[0] if price else None,
                                  price[1] if price else None))
    out.sort(key=lambda r: (-r.similarity, r.profile.release_id))
    return out[:limit]
