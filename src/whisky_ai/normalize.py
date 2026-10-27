"""商品名・容量・度数・バッチ・JAN の正規化とパース。

マーケットプレイスの商品名は「【送料無料】」「箱付き」「正規品」等のノイズや
全角/半角・cl/L/ml の表記ゆれを含む。ここで比較可能な形に揃える。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# 照合に寄与しない販促語。商品の同一性 (容量・度数・年数・バッチ) に関わる語は含めないこと。
_NOISE_WORDS = (
    "送料無料", "送料込み", "送料込", "正規品", "正規", "並行輸入品", "並行品", "箱付き", "箱付",
    "化粧箱入り", "化粧箱付", "カートン付", "箱入り", "ギフト", "プレゼント", "お中元", "お歳暮",
    "父の日", "母の日", "敬老の日", "あす楽", "ポイント", "倍", "新品", "未開栓", "数量限定",
    "限定品", "在庫あり", "ランキング", "楽天", "入賞", "即納", "ラッピング", "のし", "対応",
    "official", "genuine",
)
_BRACKETS_RE = re.compile(r"[【\[［(（<＜「『]([^】\]］)）>＞」』]*)[】\]］)）>＞」』]")
_SEPARATORS_RE = re.compile(r"[\s/・･,，、。|｜:：;；~〜\-‐–—_]+")

_VOLUME_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(ml|mℓ|ミリリットル|cl|センチリットル|l|ℓ|リットル)(?![a-z])",
    re.IGNORECASE,
)
_ABV_RE = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d{1,2})?)\s*(%|％|度|percent|abv|vol)", re.IGNORECASE)
_ABV_PREFIX_RE = re.compile(r"(?:abv|alc\.?|アルコール度数?)\s*(\d{1,2}(?:\.\d{1,2})?)", re.IGNORECASE)
_AGE_RE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:年|years?\s*old|years?|yrs?|yo|y\.o\.)(?![a-z\d])", re.IGNORECASE
)
_BATCH_RE = re.compile(
    r"(?:batch|バッチ|ロット|lot|cask\s*no\.?|カスク\s*no\.?|カスク番号)\s*(?:no\.?|ナンバー|#)?\s*"
    r"([a-z]?\d{1,5}[a-z]?)",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    """NFKC・小文字化・長音/記号の統一・空白の圧縮。表記ゆれ比較の基礎。"""
    s = unicodedata.normalize("NFKC", text).lower()
    s = s.replace("ー", "ー").replace("―", "ー").replace("‐", "-")
    s = s.replace("’", "'").replace("`", "'")
    return re.sub(r"\s+", " ", s).strip()


def strip_noise(text: str) -> str:
    """販促ブラケットとノイズ語を除去する (正規化済み文字列を想定)。"""

    def _bracket(m: re.Match[str]) -> str:
        # 中身が販促語だけの括弧 (【送料無料】等) は捨て、商品情報 ((Sherry Cask) 等) は残す
        inner = m.group(1)
        rest = inner
        for w in _NOISE_WORDS:
            rest = rest.replace(w, " ")
        return " " if not re.sub(r"[\s\d位%％!！]+", "", rest) else f" {inner} "

    s = _BRACKETS_RE.sub(_bracket, text)
    for w in _NOISE_WORDS:
        s = s.replace(w, " ")
    return re.sub(r"\s+", " ", s).strip()


def parse_volume_ml(text: str) -> int | None:
    """'700ml' '70cl' '0.7L' '1.75l' などを ml 整数に変換。複数ある場合は最初の値。"""
    m = _VOLUME_RE.search(normalize_text(text))
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ("cl", "センチリットル"):
        value *= 10
    elif unit in ("l", "ℓ", "リットル"):
        value *= 1000
    ml = round(value)
    return ml if 1 <= ml <= 20000 else None


def parse_abv_bp(text: str) -> int | None:
    """'43%' '43度' 'ABV 46.3' を 1/100% 単位 (4300) に変換。"""
    s = normalize_text(text)
    m = _ABV_RE.search(s) or _ABV_PREFIX_RE.search(s)
    if not m:
        return None
    value = float(m.group(1))
    if not 0 < value <= 96:
        return None
    return round(value * 100)


def parse_age_years(text: str) -> int | None:
    m = _AGE_RE.search(normalize_text(text))
    if not m:
        return None
    age = int(m.group(1))
    # 「2024年」のような西暦は _AGE_RE の (?<![\d.]) と 2 桁制限で除外済み
    return age if 3 <= age <= 80 else None


def normalize_batch_code(code: str) -> str:
    """'No.03' 'batch 3' '#3' → '3'。英字付きは大文字 ('a2' → 'A2')。"""
    s = normalize_text(code)
    s = re.sub(r"^(batch|バッチ|lot|ロット|no\.?|#)\s*", "", s)
    s = re.sub(r"\s+", "", s).upper()
    m = re.fullmatch(r"([A-Z]?)0*(\d+)([A-Z]?)", s)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    return s


def parse_batch_code(text: str) -> str | None:
    m = _BATCH_RE.search(normalize_text(text))
    return normalize_batch_code(m.group(1)) if m else None


def name_tokens(text: str) -> list[str]:
    """容量・度数・年数・バッチ表記を取り除いた名前トークン。"""
    s = strip_noise(normalize_text(text))
    for rx in (_VOLUME_RE, _ABV_RE, _ABV_PREFIX_RE, _BATCH_RE, _AGE_RE):
        s = rx.sub(" ", s)
    tokens = [t for t in _SEPARATORS_RE.split(s) if t]
    return [t for t in tokens if t not in ("whisky", "whiskey", "ウイスキー", "ウィスキー")]


def name_key(text: str) -> str:
    return " ".join(name_tokens(text))


# ===== JAN / EAN / UPC =====


def gtin_check_digit_ok(code: str) -> bool:
    """GTIN-8/12/13/14 のチェックディジット検証 (モジュラス10 ウェイト3-1)。"""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


@dataclass(frozen=True)
class Gtin:
    id_type: str  # 'jan13' | 'jan8' | 'upc12' | 'itf14'
    value: str


def parse_gtin(raw: str | None) -> Gtin | None:
    """生の JAN 文字列を検証・正規化。UPC-A は先頭 0 付きの 13 桁として扱う。

    チェックディジット不正の場合は None (照合に使わない)。
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", unicodedata.normalize("NFKC", raw))
    if not gtin_check_digit_ok(digits):
        return None
    if len(digits) == 12:
        return Gtin("jan13", "0" + digits)  # UPC-A → EAN-13 表現で一本化
    if len(digits) == 13:
        return Gtin("jan13", digits)
    if len(digits) == 8:
        return Gtin("jan8", digits)
    return Gtin("itf14", digits)


def make_gtin13(body12: str) -> str:
    """12 桁の本体にチェックディジットを付けて 13 桁にする (テスト・サンプル用)。"""
    if not (body12.isdigit() and len(body12) == 12):
        raise ValueError("body must be 12 digits")
    for d in "0123456789":
        if gtin_check_digit_ok(body12 + d):
            return body12 + d
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class ParsedTitle:
    raw: str
    name_key: str
    tokens: tuple[str, ...]
    volume_ml: int | None
    abv_bp: int | None
    age_years: int | None
    batch_code: str | None


def parse_title(title: str) -> ParsedTitle:
    tokens = name_tokens(title)
    return ParsedTitle(
        raw=title,
        name_key=" ".join(tokens),
        tokens=tuple(tokens),
        volume_ml=parse_volume_ml(title),
        abv_bp=parse_abv_bp(title),
        age_years=parse_age_years(title),
        batch_code=parse_batch_code(title),
    )
