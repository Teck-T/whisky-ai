"""商品同一性判定のテスト。

架空サンプルカタログ (62 銘柄 / 140+ ボトル) を対象に、
同名異商品・容量違い・度数違い・バッチ違い・JAN の誤りを検証する。
"""

from __future__ import annotations

import pytest

from whisky_ai.matching import Decision, ListingInput, Matcher
from whisky_ai.normalize import make_gtin13
from whisky_ai.sample_data import SampleCatalog, sample_listings


def _name(m: Matcher, bottle_id: int | None) -> str | None:
    return None if bottle_id is None else m.catalog.profiles[bottle_id].display_name


def match(m: Matcher, title: str, jan: str | None = None, **kw: object) -> tuple[Decision, str | None]:
    r = m.match(ListingInput(title, jan, **kw))  # type: ignore[arg-type]
    return r.decision, _name(m, r.bottle_id)


def test_sample_catalog_size(catalog: SampleCatalog) -> None:
    assert len(catalog.brand_ids) >= 50
    assert len(catalog.bottle_ids) >= 100


# ---------- 全サンプル出品 ----------

def test_all_sample_listings_match_by_jan(matcher: Matcher, catalog: SampleCatalog) -> None:
    items = sample_listings(catalog)
    assert len(items) >= 50
    for item in items:
        assert match(matcher, item.title, item.jan) == (Decision.MATCHED, item.expected), item.title


def test_name_only_never_matches_wrong_bottle(matcher: Matcher, catalog: SampleCatalog) -> None:
    """JAN なしでも誤照合ゼロ。自動照合できないものは要確認になる。"""
    items = sample_listings(catalog)
    matched = 0
    for item in items:
        decision, name = match(matcher, item.title)
        assert decision in (Decision.MATCHED, Decision.NEEDS_REVIEW), item.title
        if decision is Decision.MATCHED:
            assert name == item.expected, item.title
            matched += 1
    assert matched / len(items) >= 0.8


def test_japanese_titles(matcher: Matcher) -> None:
    assert match(matcher, "青川 12年 シングルモルト 700ml 箱付") == (
        Decision.MATCHED, "Aokawa 12 Years 700ml")
    assert match(matcher, "【楽天ランキング1位】星空 シェリー樽 12年 700ml") == (
        Decision.MATCHED, "Hoshizora Sherry Cask 12 Years 700ml")


# ---------- 同名異商品 ----------

@pytest.mark.parametrize(("title", "expected"), [
    ("Aokawa Original 700ml 43%", "Aokawa Original 700ml"),
    ("Aokawa Blended Original 700ml", "Aokawa Blended Original 700ml"),
    ("Hoshizora 12年 700ml", "Hoshizora 12 Years 700ml"),
    ("Hoshizora Sherry Cask 12年 700ml", "Hoshizora Sherry Cask 12 Years 700ml"),
    ("Aokawa 18年 700ml", "Aokawa 18 Years 700ml"),
    ("Aokawa Harbour 17年 700ml", "Aokawa Harbour 17 Years 700ml"),
])
def test_same_name_different_products(matcher: Matcher, title: str, expected: str) -> None:
    assert match(matcher, title) == (Decision.MATCHED, expected)


def test_brand_only_title_is_review(matcher: Matcher) -> None:
    decision, _ = match(matcher, "Aokawa 700ml")
    assert decision is Decision.NEEDS_REVIEW


def test_age_without_release_in_catalog_is_not_matched(matcher: Matcher) -> None:
    # Shirakaba は 12 年のみ。18 年はカタログ外 → 12 年に寄せてはいけない
    decision, name = match(matcher, "Shirakaba 18年 700ml")
    assert decision is not Decision.MATCHED and name is None


def test_unknown_product(matcher: Matcher) -> None:
    assert match(matcher, "Completely Different Gin 700ml") == (Decision.NO_MATCH, None)


# ---------- 容量違い ----------

@pytest.mark.parametrize(("title", "expected"), [
    ("Aokawa Original 700ml", "Aokawa Original 700ml"),
    ("Aokawa Original 180ml", "Aokawa Original 180ml"),
    ("Aokawa Original ミニチュア 50ml", "Aokawa Original 50ml"),
    ("Aokawa Original 70cl", "Aokawa Original 700ml"),
])
def test_volume_variants(matcher: Matcher, title: str, expected: str) -> None:
    assert match(matcher, title) == (Decision.MATCHED, expected)


def test_volume_missing_is_review(matcher: Matcher) -> None:
    r = matcher.match(ListingInput("Aokawa Original"))
    assert r.decision is Decision.NEEDS_REVIEW
    assert "volume_unknown" in r.reasons
    assert {_name(matcher, c.bottle_id) for c in r.candidates} >= {
        "Aokawa Original 700ml", "Aokawa Original 180ml", "Aokawa Original 50ml"}


def test_volume_not_in_catalog_is_not_matched(matcher: Matcher) -> None:
    decision, _ = match(matcher, "Aokawa Original 1000ml")
    assert decision is Decision.NO_MATCH


def test_jan_with_conflicting_volume_is_review(matcher: Matcher, catalog: SampleCatalog) -> None:
    jan_700 = catalog.jans["Aokawa Original 700ml"]
    r = matcher.match(ListingInput("Aokawa Original 180ml", jan_700))
    assert r.decision is Decision.NEEDS_REVIEW
    assert "jan_match_but_spec_conflict" in r.reasons


# ---------- 度数違い ----------

def test_abv_variants(matcher: Matcher) -> None:
    assert match(matcher, "Mistral Peak Original 700ml 40%") == (
        Decision.MATCHED, "Mistral Peak Original 700ml 40%")
    assert match(matcher, "ミストラルピーク オリジナル 700ml 43度") == (
        Decision.MATCHED, "Mistral Peak Original 700ml 43%")


def test_abv_missing_with_variants_is_review(matcher: Matcher) -> None:
    r = matcher.match(ListingInput("Mistral Peak Original 700ml"))
    assert r.decision is Decision.NEEDS_REVIEW
    assert "ambiguous_multiple_candidates" in r.reasons
    assert len(r.candidates) == 2


def test_structured_abv_overrides_title(matcher: Matcher) -> None:
    assert match(matcher, "Mistral Peak Original 700ml", abv_bp=4000) == (
        Decision.MATCHED, "Mistral Peak Original 700ml 40%")


# ---------- バッチ違い ----------

def test_shared_jan_without_batch_is_review(matcher: Matcher, catalog: SampleCatalog) -> None:
    jan = catalog.jans["Kurogane Cask Strength Batch 1 700ml"]
    assert jan == catalog.jans["Kurogane Cask Strength Batch 2 700ml"]
    r = matcher.match(ListingInput("黒鉄 カスクストレングス 700ml", jan))
    assert r.decision is Decision.NEEDS_REVIEW
    assert "jan_shared_by_multiple_bottles" in r.reasons
    assert len(r.candidates) == 2


@pytest.mark.parametrize(("title", "expected"), [
    ("黒鉄 カスクストレングス バッチ2 700ml", "Kurogane Cask Strength Batch 2 700ml"),
    ("Kurogane Cask Strength Batch No.1 700ml", "Kurogane Cask Strength Batch 1 700ml"),
    ("Kurogane Cask Strength 700ml 57.6%", "Kurogane Cask Strength Batch 2 700ml"),  # 度数で一意
])
def test_shared_jan_resolved_by_batch_or_abv(
    matcher: Matcher, catalog: SampleCatalog, title: str, expected: str
) -> None:
    jan = catalog.jans["Kurogane Cask Strength Batch 1 700ml"]
    assert match(matcher, title, jan) == (Decision.MATCHED, expected)


def test_batch_by_name_only(matcher: Matcher) -> None:
    assert match(matcher, "Kurogane Cask Strength Batch 3 700ml") == (
        Decision.MATCHED, "Kurogane Cask Strength Batch 3 700ml")
    r = matcher.match(ListingInput("Kurogane Cask Strength 700ml"))
    assert r.decision is Decision.NEEDS_REVIEW and "batch_unknown" in r.reasons


def test_batch_conflicting_with_jan_is_review(matcher: Matcher, catalog: SampleCatalog) -> None:
    jan3 = catalog.jans["Kurogane Cask Strength Batch 3 700ml"]
    r = matcher.match(ListingInput("Kurogane Cask Strength Batch 1 700ml", jan3))
    assert r.decision is Decision.NEEDS_REVIEW


def test_unknown_batch_not_matched(matcher: Matcher) -> None:
    decision, _ = match(matcher, "Kurogane Cask Strength Batch 9 700ml")
    assert decision is not Decision.MATCHED


def test_kurogane_brand_vs_cask_strength_brand(matcher: Matcher) -> None:
    assert match(matcher, "Kurogane 12年 700ml") == (Decision.MATCHED, "Kurogane 12 Years 700ml")


# ---------- JAN の誤り ----------

def test_invalid_jan_is_ignored_not_trusted(matcher: Matcher, catalog: SampleCatalog) -> None:
    good = catalog.jans["Aokawa 12 Years 700ml"]
    bad = good[:-1] + str((int(good[-1]) + 1) % 10)
    r = matcher.match(ListingInput("Aokawa 12年 700ml", bad))
    assert "jan_invalid_ignored" in r.reasons
    assert r.decision is Decision.MATCHED and r.method == "name_spec"


def test_jan_pointing_to_unrelated_product_is_review(matcher: Matcher, catalog: SampleCatalog) -> None:
    jan = catalog.jans["Ardvellan 12 Years 700ml"]
    r = matcher.match(ListingInput("Completely Different Gin 700ml", jan))
    assert r.decision is Decision.NEEDS_REVIEW
    assert "jan_match_but_name_mismatch" in r.reasons


def test_unknown_jan_differs_from_catalog_is_review(matcher: Matcher) -> None:
    other_jan = make_gtin13("299999999999")
    r = matcher.match(ListingInput("Aokawa 12年 700ml", other_jan))
    assert r.decision is Decision.NEEDS_REVIEW
    assert "jan_differs_from_catalog" in r.reasons
