"""架空のサンプルデータ。**実在の蒸留所・銘柄・価格ではない。**

* 蒸留所名・銘柄名はすべて創作。
* JAN は GS1 のインストアコード領域 (先頭 '2') を使い、実在商品の JAN と衝突しない。
* 定価 (MSRP) は ``kind='sample'`` の出典に紐付けた架空値で、実在商品の定価ではない。

照合テスト用に、以下の「紛らわしいケース」を意図的に含む:

* 同名異商品: 「Aokawa」(シングルモルト) と「Aokawa Blended」
* 容量違い: 同一リリースの 700ml / 180ml / 50ml
* 度数違い: 「Mistral Peak Original」40% (輸出向け) と 43% (国内向け)
* バッチ違い: 「Kurogane Cask Strength」Batch 1/2/3。Batch 1 と 2 は JAN を共有
* 派生リリース: 「Hoshizora 12 Years」と「Hoshizora Sherry Cask 12 Years」
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from itertools import count

from whisky_ai import repository as repo
from whisky_ai.normalize import make_gtin13


@dataclass(frozen=True)
class DistillerySpec:
    name: str
    name_ja: str
    country: str
    region: str | None
    default_abv_bp: int


DISTILLERIES: tuple[DistillerySpec, ...] = (
    DistillerySpec("Aokawa", "青川", "Japan", "Nagano", 4300),
    DistillerySpec("Kurogane", "黒鉄", "Japan", "Hokkaido", 4600),
    DistillerySpec("Hoshizora", "星空", "Japan", "Yamanashi", 4300),
    DistillerySpec("Shirakaba", "白樺", "Japan", "Iwate", 4000),
    DistillerySpec("Glen Corrach", "グレンコラック", "Scotland", "Speyside", 4000),
    DistillerySpec("Ardvellan", "アードヴェラン", "Scotland", "Islay", 4600),
    DistillerySpec("Loch Saorsa", "ロッホサオルサ", "Scotland", "Highland", 4300),
    DistillerySpec("Ballymorrow", "バリーモロウ", "Ireland", None, 4000),
    DistillerySpec("Copper Hollow", "カッパーホロウ", "USA", "Kentucky", 4500),
    DistillerySpec("Tasman Ridge", "タスマンリッジ", "Australia", "Tasmania", 4600),
    DistillerySpec("Kirinuki", "切貫", "Japan", "Kagoshima", 4300),
    DistillerySpec("Inverbrae", "インヴァーブレイ", "Scotland", "Lowland", 4000),
)

# (ブランド名の接尾辞, 日本語接尾辞, カテゴリ)
LINES: tuple[tuple[str, str, str], ...] = (
    ("", "", "single_malt"),
    (" Blended", " ブレンデッド", "blended"),
    (" Reserve", " リザーブ", "blended_malt"),
    (" Harbour", " ハーバー", "blended"),
    (" Founders", " ファウンダーズ", "single_grain"),
)


@dataclass
class SampleCatalog:
    """生成結果の参照 (テストで使う)。"""

    source_id: int
    brand_ids: dict[str, int] = field(default_factory=dict)
    release_ids: dict[str, int] = field(default_factory=dict)
    bottle_ids: dict[str, int] = field(default_factory=dict)  # key: display_name
    jans: dict[str, str] = field(default_factory=dict)        # display_name -> JAN


class _JanFactory:
    """インストアコード '29' + 連番10桁 + チェックディジット。"""

    def __init__(self) -> None:
        self._seq = count(1)

    def next(self) -> str:
        return make_gtin13(f"29{next(self._seq):010d}")


def _bottle(
    conn: sqlite3.Connection, cat: SampleCatalog, jans: _JanFactory, release_id: int,
    display: str, volume_ml: int, abv_bp: int, *, batch_id: int | None = None,
    jan: str | None = None, msrp: int | None = None,
) -> int:
    bottle_id = repo.add_bottle(conn, release_id, volume_ml, abv_bp, display, cat.source_id,
                                batch_id=batch_id)
    code = jan or jans.next()
    repo.add_identifier(conn, bottle_id, code, cat.source_id)
    if msrp is not None:
        repo.add_retail_price_reference(conn, bottle_id, msrp, tax_included=True,
                                        source_id=cat.source_id)
    cat.bottle_ids[display] = bottle_id
    cat.jans[display] = code
    return bottle_id


def load_sample_catalog(conn: sqlite3.Connection) -> SampleCatalog:
    """サンプルカタログを投入する (空の DB を想定)。"""
    with conn:
        source_id = repo.add_source(
            conn, "sample", "架空サンプルデータ (実在商品ではない)",
            note="whisky_ai.sample_data で生成。蒸留所・銘柄・JAN・価格はすべて架空。",
        )
        cat = SampleCatalog(source_id)
        jans = _JanFactory()

        dist_ids = []
        for d in DISTILLERIES:
            dist_ids.append(repo.add_distillery(
                conn, f"{d.name} Distillery", d.country, source_id,
                name_ja=f"{d.name_ja}蒸溜所", region=d.region,
            ))

        for di, d in enumerate(DISTILLERIES):
            for li, (suffix, suffix_ja, category) in enumerate(LINES):
                brand = f"{d.name}{suffix}"
                brand_ja = f"{d.name_ja}{suffix_ja}"
                brand_id = repo.add_brand(conn, brand, category, source_id, name_ja=brand_ja,
                                          owner=f"{d.name} Spirits (fictional)",
                                          distillery_ids=(dist_ids[di],))
                cat.brand_ids[brand] = brand_id
                abv = d.default_abv_bp if li == 0 else 4000
                base_price = 3000 + 400 * di + 500 * (li == 0)

                rel = repo.add_release(conn, brand_id, "Original", source_id, name_ja="オリジナル")
                repo.add_alias(conn, "release", rel, "NAS", lang="en")
                cat.release_ids[f"{brand} Original"] = rel
                _bottle(conn, cat, jans, rel, f"{brand} Original 700ml", 700, abv,
                        msrp=base_price if di % 3 == 0 else None)
                if li == 0 or di % 2 == 0:
                    _bottle(conn, cat, jans, rel, f"{brand} Original 180ml", 180, abv)
                if li == 0 and di % 2 == 0:
                    _bottle(conn, cat, jans, rel, f"{brand} Original 50ml", 50, abv)

                if li == 0:
                    for age in (12, 18) if di % 2 == 0 else (12,):
                        rel = repo.add_release(conn, brand_id, f"{age} Years", source_id,
                                               name_ja=f"{age}年", age_years=age)
                        cat.release_ids[f"{brand} {age} Years"] = rel
                        _bottle(conn, cat, jans, rel, f"{brand} {age} Years 700ml", 700, abv,
                                msrp=base_price * (2 if age == 12 else 5))
                    if di % 2 == 0:
                        rel = repo.add_release(conn, brand_id, "Sherry Cask 12 Years", source_id,
                                               name_ja="シェリーカスク 12年", age_years=12,
                                               cask_type="sherry")
                        repo.add_alias(conn, "release", rel, "シェリー樽 12年", lang="ja")
                        cat.release_ids[f"{brand} Sherry Cask 12 Years"] = rel
                        _bottle(conn, cat, jans, rel, f"{brand} Sherry Cask 12 Years 700ml",
                                700, abv)
                elif li == 3:
                    rel = repo.add_release(conn, brand_id, "17 Years", source_id,
                                           name_ja="17年", age_years=17)
                    cat.release_ids[f"{brand} 17 Years"] = rel
                    _bottle(conn, cat, jans, rel, f"{brand} 17 Years 700ml", 700, 4300)

        # --- 意図的な紛らわしいケース ---
        # 度数違い (同名・同容量)
        mistral = repo.add_brand(conn, "Mistral Peak", "blended", source_id,
                                 name_ja="ミストラルピーク", distillery_ids=(dist_ids[4], dist_ids[6]))
        cat.brand_ids["Mistral Peak"] = mistral
        rel = repo.add_release(conn, mistral, "Original", source_id, name_ja="オリジナル")
        cat.release_ids["Mistral Peak Original"] = rel
        _bottle(conn, cat, jans, rel, "Mistral Peak Original 700ml 40%", 700, 4000)
        _bottle(conn, cat, jans, rel, "Mistral Peak Original 700ml 43%", 700, 4300)

        # バッチ違い (Batch 1 と 2 は JAN を共有、度数はバッチごとに異なる)
        cs_brand = repo.add_brand(conn, "Kurogane Cask Strength", "single_malt", source_id,
                                  name_ja="黒鉄 カスクストレングス", distillery_ids=(dist_ids[1],))
        cat.brand_ids["Kurogane Cask Strength"] = cs_brand
        rel = repo.add_release(conn, cs_brand, "Small Batch", source_id, name_ja="スモールバッチ",
                               is_limited=True, has_batches=True)
        # 出品名ではリリース名「Small Batch」が省略されがちなため、ブランド名だけで一致できる別名を登録
        repo.add_alias(conn, "release", rel, "Cask Strength", lang="en")
        repo.add_alias(conn, "release", rel, "カスクストレングス", lang="ja")
        cat.release_ids["Kurogane Cask Strength Small Batch"] = rel
        shared_jan = jans.next()
        for code, year, abv_bp, jan in (("1", 2023, 5820, shared_jan), ("2", 2024, 5760, shared_jan),
                                        ("3", 2025, 5910, None)):
            batch_id = repo.add_batch(conn, rel, code, source_id, batch_label=f"Batch No.{code}",
                                      bottling_year=year, outturn=3000)
            _bottle(conn, cat, jans, rel, f"Kurogane Cask Strength Batch {code} 700ml", 700, abv_bp,
                    batch_id=batch_id, jan=jan)
    return cat


# ===== モック出品 (価格収集 PR 前の動作確認用) =====

@dataclass(frozen=True)
class SampleListing:
    external_item_id: str
    title: str
    jan: str | None
    price_jpy: int
    expected: str | None  # 期待される照合先 display_name (None = 要確認/不一致が正解)


def sample_listings(cat: SampleCatalog) -> list[SampleListing]:
    """販促語・表記ゆれを含むモック出品タイトル。"""
    out: list[SampleListing] = []
    n = count(1)
    for display, jan in cat.jans.items():
        if not display.endswith("700ml") or "Batch" in display:
            continue
        name = display.removesuffix(" 700ml")
        out.append(SampleListing(f"mock-{next(n):04d}", f"【送料無料】{name} 700ml 正規品 箱付き",
                                 jan, 4000, display))
    return out
