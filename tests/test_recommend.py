from __future__ import annotations

import sqlite3

import pytest

from whisky_ai import repository as repo
from whisky_ai.collectors.base import SearchQuery
from whisky_ai.collectors.factory import build_collector
from whisky_ai.collectors.pipeline import collect
from whisky_ai.flavor import store_extraction
from whisky_ai.recommend import load_profiles, parse_taste, recommend, similarity
from whisky_ai.sample_data import SampleCatalog, load_sample_flavors


def _payload(**axes: float) -> dict[str, object]:
    return {"schema_version": "1", "nose": [{"descriptor": "バニラ"}], "palate": [], "finish": [],
            "axes": axes}


def test_similarity_math() -> None:
    a = {"sweetness": 0.8, "smokiness": 0.2, "body": 0.5}
    assert similarity(a, a) == (1.0, 3)
    # 共通 3 軸で差 0、和集合 4 軸 → 0.75 (片方にしかない sherry を減点)
    assert similarity(a, {**a, "sherry": 0.9}) == (0.75, 3)
    # 好みの指定との比較では、相手の余分な軸で減点しない
    assert similarity(a, {**a, "sherry": 0.9}, query=True) == (1.0, 3)
    assert similarity({"sweetness": 1.0}, {"sweetness": 0.0}) == (0.0, 1)
    assert similarity({"sweetness": 1.0}, {"body": 0.5}) == (0.0, 0)


def test_parse_taste() -> None:
    assert parse_taste("sweetness=0.8, smokiness=0.2") == {"sweetness": 0.8, "smokiness": 0.2}
    for bad in ("umami=0.5", "sweetness=1.5", "", "sweetness=abc"):
        with pytest.raises(ValueError):
            parse_taste(bad)


def test_profiles_aggregate_via_bottle_review_and_skip_invalid(
    conn: sqlite3.Connection, catalog: SampleCatalog
) -> None:
    rel = catalog.release_ids["Aokawa 12 Years"]
    bottle = catalog.bottle_ids["Aokawa 12 Years 700ml"]
    store_extraction(conn, _payload(sweetness=0.6, body=0.4), model_name="m", prompt_version="p",
                     release_id=rel)
    store_extraction(conn, _payload(sweetness=0.8), model_name="m", prompt_version="p", bottle_id=bottle)
    with conn:
        src = repo.add_source(conn, "manual_entry", "note")
        rid = conn.execute("INSERT INTO reviews (bottle_id, source_id, body, created_at)"
                           " VALUES (?, ?, 'x', 'now')", (bottle, src)).lastrowid
    store_extraction(conn, _payload(sweetness=1.0), model_name="m", prompt_version="p", review_id=rid)
    store_extraction(conn, _payload(sweetness=5.0), model_name="m", prompt_version="p", release_id=rel)
    prof = load_profiles(conn)[rel]
    assert prof.n_extractions == 3
    assert prof.axes == {"sweetness": 0.8, "body": 0.4}
    assert prof.top_notes == ["バニラ"]


def test_recommend_like_and_taste(conn: sqlite3.Connection, catalog: SampleCatalog) -> None:
    load_sample_flavors(conn, catalog)
    islay = catalog.release_ids["Ardvellan 12 Years"]
    recs = recommend(conn, like_release_id=islay, limit=3)
    assert islay not in [r.profile.release_id for r in recs]
    assert all(r.profile.name.startswith("Ardvellan") for r in recs)  # 架空のスモーキー系が上位
    assert [r.similarity for r in recs] == sorted((r.similarity for r in recs), reverse=True)

    sherry = recommend(conn, taste={"sherry": 0.9, "sweetness": 0.7, "smokiness": 0.1}, limit=3)
    assert all("Sherry" in r.profile.name for r in sherry)

    with pytest.raises(ValueError):
        recommend(conn)
    with pytest.raises(KeyError):
        recommend(conn, like_release_id=999999)


def test_recommend_with_prices_and_budget(conn: sqlite3.Connection, catalog: SampleCatalog) -> None:
    load_sample_flavors(conn, catalog)
    collect(conn, build_collector("yahoo", env={}), SearchQuery(keyword="700ml"))
    collect(conn, build_collector("rakuten", env={}), SearchQuery(keyword="700ml"))
    aokawa12 = catalog.release_ids["Aokawa 12 Years"]
    recs = recommend(conn, taste={"sweetness": 0.6, "fruitiness": 0.6, "floral": 0.5}, limit=50)
    by_id = {r.profile.release_id: r for r in recs}
    # Aokawa 12: Yahoo! 6,200 円 / 楽天 6,380 円 (税込) / 楽天 5,800 円 (税抜表示 → 比較対象外)
    assert by_id[aokawa12].best_price_jpy == 6200
    cheap = recommend(conn, taste={"sweetness": 0.6}, max_price_jpy=6000, limit=50)
    assert cheap and all(r.best_price_jpy is not None and r.best_price_jpy <= 6000 for r in cheap)
    assert aokawa12 not in {r.profile.release_id for r in cheap}
