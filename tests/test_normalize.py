from __future__ import annotations

import pytest

from whisky_ai.normalize import (
    gtin_check_digit_ok,
    make_gtin13,
    name_key,
    normalize_batch_code,
    parse_abv_bp,
    parse_age_years,
    parse_batch_code,
    parse_gtin,
    parse_volume_ml,
)


@pytest.mark.parametrize(("text", "ml"), [
    ("Aokawa 700ml", 700), ("７００ＭＬ", 700), ("70cl", 700), ("0.7L", 700), ("1.75l", 1750),
    ("180ml 43%", 180), ("50 ml", 50), ("1リットル", 1000), ("12年", None), ("Aokawa", None),
])
def test_parse_volume(text: str, ml: int | None) -> None:
    assert parse_volume_ml(text) == ml


@pytest.mark.parametrize(("text", "bp"), [
    ("43%", 4300), ("43度", 4300), ("５７．６％", 5760), ("ABV 46.3", 4630), ("700ml", None),
    ("58.65% vol", 5865),
])
def test_parse_abv(text: str, bp: int | None) -> None:
    assert parse_abv_bp(text) == bp


@pytest.mark.parametrize(("text", "age"), [
    ("12年", 12), ("12 Years Old", 12), ("18yo", 18), ("２１年", 21), ("2024年ボトリング", None),
    ("Original", None),
])
def test_parse_age(text: str, age: int | None) -> None:
    assert parse_age_years(text) == age


@pytest.mark.parametrize(("text", "code"), [
    ("Batch 3", "3"), ("バッチ2", "2"), ("Batch No.03", "3"), ("ロット #A12", "A12"), ("Original", None),
])
def test_parse_batch(text: str, code: str | None) -> None:
    assert parse_batch_code(text) == code


def test_normalize_batch_code() -> None:
    assert normalize_batch_code("No.007") == "7"
    assert normalize_batch_code("a2") == "A2"


def test_name_key_strips_noise_and_specs() -> None:
    assert name_key("【送料無料】Aokawa 12年 700ml 43% 正規品 箱付き") == "aokawa"
    assert name_key("Hoshizora (Sherry Cask) 12 Years") == "hoshizora sherry cask"


def test_gtin_check_digit() -> None:
    assert gtin_check_digit_ok("4901777018686")
    assert not gtin_check_digit_ok("4901777018687")
    assert gtin_check_digit_ok("49123456")  # GTIN-8


def test_parse_gtin_normalizes_upc_and_rejects_bad() -> None:
    assert parse_gtin("036000291452") is not None
    assert parse_gtin("036000291452").value == "0036000291452"  # type: ignore[union-attr]
    assert parse_gtin("4901777018687") is None
    assert parse_gtin("４９０１７７７０１８６８６").value == "4901777018686"  # type: ignore[union-attr]
    assert parse_gtin("") is None


def test_make_gtin13() -> None:
    code = make_gtin13("290000000001")
    assert len(code) == 13 and gtin_check_digit_ok(code)
