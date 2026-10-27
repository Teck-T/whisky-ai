"""販売商品 (listing) とボトル (SKU) の同一性判定。

判定方針 (docs/design.md 「商品同一性判定」):

* 仕様の矛盾 (容量・度数・熟成年数・バッチ) は「ハード制約」。矛盾する候補は除外する。
* JAN は強い証拠だが絶対ではない。JAN の使い回し (バッチ違いで同一 JAN) や
  出品者の誤入力があるため、仕様や名前と矛盾すれば要確認にする。
* 名前のみでの自動照合は、スコア・次点との差・容量の明示・年数/バッチの明示が
  すべて揃った場合に限る。それ以外は ``NEEDS_REVIEW`` として候補を保存し、
  人が確定するまで listing を bottle に紐付けない。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum

from whisky_ai.db import utcnow
from whisky_ai.normalize import Gtin, name_tokens, normalize_batch_code, parse_gtin, parse_title


class Decision(StrEnum):
    MATCHED = "matched"
    NEEDS_REVIEW = "pending_review"
    NO_MATCH = "unmatched"


@dataclass(frozen=True)
class MatchConfig:
    auto_threshold: float = 0.90   # 名前のみ自動照合の最低スコア
    min_margin: float = 0.05       # 次点との最小差
    review_threshold: float = 0.50  # 要確認候補として残す最低スコア
    max_candidates: int = 5
    jan_min_name_score: float = 0.34  # JAN 一致でも名前がこれ未満なら要確認
    # 出品名に「他候補だけが持つ語」(例: Sherry, Blended) があるときの減点
    sibling_token_penalty: float = 0.15


@dataclass(frozen=True)
class BottleProfile:
    bottle_id: int
    release_id: int
    display_name: str
    volume_ml: int
    abv_bp: int
    age_years: int | None
    batch_code: str | None
    release_has_batches: bool
    name_variants: tuple[tuple[str, ...], ...]
    gtins: frozenset[str]

    @property
    def all_tokens(self) -> frozenset[str]:
        return frozenset(t for v in self.name_variants for t in v)


@dataclass(frozen=True)
class ListingInput:
    """照合対象。構造化フィールドがあればタイトルからのパース値より優先する。"""

    title: str
    jan_raw: str | None = None
    volume_ml: int | None = None
    abv_bp: int | None = None
    age_years: int | None = None
    batch_code: str | None = None


@dataclass
class Candidate:
    bottle_id: int
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class MatchResult:
    decision: Decision
    bottle_id: int | None
    method: str | None
    score: float
    candidates: list[Candidate]
    reasons: list[str]
    spec: dict[str, object]


class Catalog:
    """照合用にボトル情報をメモリへ展開したもの。"""

    def __init__(self, profiles: list[BottleProfile]) -> None:
        self.profiles = {p.bottle_id: p for p in profiles}
        self.by_gtin: dict[str, list[BottleProfile]] = defaultdict(list)
        for p in profiles:
            for g in p.gtins:
                self.by_gtin[g].append(p)

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> Catalog:
        aliases: dict[tuple[str, int], set[str]] = defaultdict(set)
        for row in conn.execute("SELECT entity_type, entity_id, alias FROM aliases"):
            aliases[(row["entity_type"], row["entity_id"])].add(row["alias"])
        gtins: dict[int, set[str]] = defaultdict(set)
        for row in conn.execute("SELECT bottle_id, value FROM product_identifiers"):
            gtins[row["bottle_id"]].add(row["value"])

        rows = conn.execute(
            """
            SELECT b.id AS bottle_id, b.release_id, b.display_name, b.volume_ml, b.abv_bp,
                   r.name AS release_name, r.name_ja AS release_name_ja, r.age_years,
                   r.has_batches, br.id AS brand_id, br.name AS brand_name,
                   br.name_ja AS brand_name_ja, bt.batch_code
            FROM bottles b
            JOIN releases r ON r.id = b.release_id
            JOIN brands br ON br.id = r.brand_id
            LEFT JOIN batches bt ON bt.id = b.batch_id
            """
        ).fetchall()
        profiles = []
        for row in rows:
            brand_names = {row["brand_name"], *aliases[("brand", row["brand_id"])]}
            if row["brand_name_ja"]:
                brand_names.add(row["brand_name_ja"])
            release_names = {row["release_name"], *aliases[("release", row["release_id"])]}
            if row["release_name_ja"]:
                release_names.add(row["release_name_ja"])
            variants = {tuple(name_tokens(f"{b} {r}")) for b in brand_names for r in release_names}
            variants.add(tuple(name_tokens(row["display_name"])))
            profiles.append(
                BottleProfile(
                    bottle_id=row["bottle_id"],
                    release_id=row["release_id"],
                    display_name=row["display_name"],
                    volume_ml=row["volume_ml"],
                    abv_bp=row["abv_bp"],
                    age_years=row["age_years"],
                    batch_code=row["batch_code"],
                    release_has_batches=bool(row["has_batches"]),
                    name_variants=tuple(sorted(v for v in variants if v)),
                    gtins=frozenset(gtins[row["bottle_id"]]),
                )
            )
        return cls(profiles)


def _present(token: str, compact: str, token_set: set[str]) -> bool:
    # 日本語は空白区切りされないことが多いので、2 文字以上は部分文字列一致で判定する
    return token in compact if len(token) >= 2 else token in token_set


def name_score(listing_tokens: tuple[str, ...], variant: tuple[str, ...]) -> float:
    """カタログ名の各トークンが出品名に含まれる割合 (再現率) を主とし、
    出品名のうちカタログ名で説明できる割合 (被覆率) で同点を崩す。

    再現率 1.0 なら 0.90〜1.00 の範囲になる。出品名には販促語など
    カタログにない語が混ざるため、被覆率の重みは小さくしている。
    """
    if not variant or not listing_tokens:
        return 0.0
    compact = "".join(listing_tokens)
    token_set = set(listing_tokens)
    matched = [t for t in variant if _present(t, compact, token_set)]
    recall = len(matched) / len(variant)
    coverage = min(1.0, sum(len(t) for t in matched) / len(compact))
    return round(recall * (0.90 + 0.10 * coverage), 4)


def _conflicts(spec: dict[str, object], p: BottleProfile) -> list[str]:
    out = []
    if spec["volume_ml"] is not None and spec["volume_ml"] != p.volume_ml:
        out.append(f"volume_conflict:{spec['volume_ml']}!={p.volume_ml}")
    if spec["abv_bp"] is not None and spec["abv_bp"] != p.abv_bp:
        out.append(f"abv_conflict:{spec['abv_bp']}!={p.abv_bp}")
    if spec["age_years"] is not None and spec["age_years"] != p.age_years:
        out.append(f"age_conflict:{spec['age_years']}!={p.age_years}")
    if spec["batch_code"] is not None and p.batch_code is not None and spec["batch_code"] != p.batch_code:
        out.append(f"batch_conflict:{spec['batch_code']}!={p.batch_code}")
    return out


def _missing_spec(spec: dict[str, object], p: BottleProfile) -> list[str]:
    """自動照合に必要だが出品側で不明な仕様。"""
    out = []
    if spec["volume_ml"] is None:
        out.append("volume_unknown")
    if p.age_years is not None and spec["age_years"] is None:
        out.append("age_unknown")
    if p.release_has_batches and spec["batch_code"] is None:
        out.append("batch_unknown")
    return out


class Matcher:
    def __init__(self, catalog: Catalog, config: MatchConfig | None = None) -> None:
        self.catalog = catalog
        self.config = config or MatchConfig()

    def _spec(self, item: ListingInput) -> tuple[dict[str, object], tuple[str, ...]]:
        parsed = parse_title(item.title)
        batch = item.batch_code if item.batch_code is not None else parsed.batch_code
        spec: dict[str, object] = {
            "volume_ml": item.volume_ml if item.volume_ml is not None else parsed.volume_ml,
            "abv_bp": item.abv_bp if item.abv_bp is not None else parsed.abv_bp,
            "age_years": item.age_years if item.age_years is not None else parsed.age_years,
            "batch_code": normalize_batch_code(batch) if batch else None,
        }
        return spec, parsed.tokens

    def _best_name_score(self, tokens: tuple[str, ...], p: BottleProfile) -> float:
        return max((name_score(tokens, v) for v in p.name_variants), default=0.0)

    def match(self, item: ListingInput) -> MatchResult:
        cfg = self.config
        spec, tokens = self._spec(item)
        reasons: list[str] = []
        gtin: Gtin | None = parse_gtin(item.jan_raw)
        if item.jan_raw and gtin is None:
            reasons.append("jan_invalid_ignored")
        spec["gtin"] = gtin.value if gtin else None

        def result(decision: Decision, cands: list[Candidate], bottle_id: int | None = None,
                   method: str | None = None, score: float = 0.0) -> MatchResult:
            cands = sorted(cands, key=lambda c: -c.score)[: cfg.max_candidates]
            if decision is Decision.NEEDS_REVIEW and not cands:
                decision = Decision.NO_MATCH
            return MatchResult(decision, bottle_id, method, score, cands, reasons, spec)

        # --- 1. JAN による照合 ---
        if gtin and gtin.value in self.catalog.by_gtin:
            jan_hits = self.catalog.by_gtin[gtin.value]
            ok: list[Candidate] = []
            bad: list[Candidate] = []
            for p in jan_hits:
                ns = self._best_name_score(tokens, p)
                conflicts = _conflicts(spec, p)
                c = Candidate(p.bottle_id, round(0.5 + 0.5 * ns, 4), ["jan_match", *conflicts])
                (bad if conflicts else ok).append(c)
            if len(ok) == 1:
                c = ok[0]
                p = self.catalog.profiles[c.bottle_id]
                ns = self._best_name_score(tokens, p)
                # 同一 JAN の他ボトルが残っていない = 明示された仕様で一意に絞り込めた
                if ns < cfg.jan_min_name_score:
                    reasons.append("jan_match_but_name_mismatch")
                    return result(Decision.NEEDS_REVIEW, ok)
                return result(Decision.MATCHED, ok, c.bottle_id, "jan", 1.0)
            if len(ok) > 1:
                reasons.append("jan_shared_by_multiple_bottles")
                return result(Decision.NEEDS_REVIEW, ok)
            reasons.append("jan_match_but_spec_conflict")
            return result(Decision.NEEDS_REVIEW, bad)

        # --- 2. 名前 + 仕様による照合 ---
        compact = "".join(tokens)
        token_set = set(tokens)
        viable: list[tuple[Candidate, BottleProfile]] = []
        for p in self.catalog.profiles.values():
            if _conflicts(spec, p):
                continue
            ns = self._best_name_score(tokens, p)
            if ns > 0:
                viable.append((Candidate(p.bottle_id, ns, ["name_spec"]), p))
        # 他の候補が持ち、この候補が持たない語が出品名にあれば減点する。
        # 例: 「Hoshizora Sherry Cask 12」は「Hoshizora 12」にも再現率 1.0 で一致するが、
        # 'sherry' は別候補のみが持つ語なので「Hoshizora 12」を下げる。
        present_by_bottle = {
            p.bottle_id: {t for t in p.all_tokens if _present(t, compact, token_set)}
            for _, p in viable
        }
        all_present = set().union(*present_by_bottle.values()) if viable else set()
        scored: list[tuple[Candidate, BottleProfile]] = []
        for c, p in viable:
            foreign = sorted(all_present - p.all_tokens)
            if foreign:
                c.score = round(c.score - cfg.sibling_token_penalty, 4)
                c.reasons.append("unexplained_tokens:" + ",".join(foreign))
            if c.score >= cfg.review_threshold:
                scored.append((c, p))
        if not scored:
            return result(Decision.NO_MATCH, [])
        scored.sort(key=lambda cp: -cp[0].score)
        best, best_p = scored[0]
        second = scored[1][0].score if len(scored) > 1 else 0.0
        cands = [c for c, _ in scored]

        blockers = _missing_spec(spec, best_p)
        if best.score < cfg.auto_threshold:
            blockers.append("score_below_auto_threshold")
        if best.score - second < cfg.min_margin:
            blockers.append("ambiguous_multiple_candidates")
        if gtin and best_p.gtins and gtin.value not in best_p.gtins:
            blockers.append("jan_differs_from_catalog")
        if blockers:
            best.reasons.extend(blockers)
            reasons.extend(blockers)
            return result(Decision.NEEDS_REVIEW, cands)
        return result(Decision.MATCHED, cands, best.bottle_id, "name_spec", best.score)


# ===== 永続化 =====


def resolve_listing(
    conn: sqlite3.Connection, listing_id: int, matcher: Matcher, *, force: bool = False
) -> MatchResult | None:
    """listing を照合し結果を保存する。手動確定済み (match_method='manual') は
    ``force=True`` でない限り再判定しない (人の判断を自動処理で上書きしない)。"""
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None:
        raise KeyError(listing_id)
    if not force and (row["match_method"] == "manual" or row["match_status"] == "rejected"):
        return None
    res = matcher.match(
        ListingInput(
            title=row["title"],
            jan_raw=row["jan_raw"],
            volume_ml=row["parsed_volume_ml"],
            abv_bp=row["parsed_abv_bp"],
            age_years=row["parsed_age_years"],
            batch_code=row["parsed_batch_code"],
        )
    )
    now = utcnow()
    with conn:
        conn.execute(
            "UPDATE match_candidates SET status = 'superseded', decided_at = ? "
            "WHERE listing_id = ? AND status = 'pending'",
            (now, listing_id),
        )
        conn.execute(
            "UPDATE listings SET bottle_id = ?, match_status = ?, match_method = ?, match_score = ? "
            "WHERE id = ?",
            (res.bottle_id, res.decision.value, res.method,
             res.score if res.decision is Decision.MATCHED else None, listing_id),
        )
        if res.decision is Decision.NEEDS_REVIEW:
            for c in res.candidates:
                payload = json.dumps({"candidate": c.reasons, "listing": res.reasons,
                                      "spec": res.spec}, ensure_ascii=False)
                conn.execute(
                    """
                    INSERT INTO match_candidates (listing_id, bottle_id, score, reasons, status, created_at)
                    VALUES (?, ?, ?, ?, 'pending', ?)
                    ON CONFLICT (listing_id, bottle_id) DO UPDATE SET
                        score = excluded.score, reasons = excluded.reasons, status = 'pending',
                        created_at = excluded.created_at, decided_at = NULL, decided_by = NULL
                    """,
                    (listing_id, c.bottle_id, c.score, payload, now),
                )
    return res


def resolve_unprocessed(conn: sqlite3.Connection, matcher: Matcher) -> dict[str, int]:
    ids = [r[0] for r in conn.execute("SELECT id FROM listings WHERE match_status = 'unprocessed'")]
    counts: dict[str, int] = defaultdict(int)
    for listing_id in ids:
        res = resolve_listing(conn, listing_id, matcher)
        if res is not None:
            counts[res.decision.value] += 1
    return dict(counts)


def accept_candidate(
    conn: sqlite3.Connection, candidate_id: int, decided_by: str, *, register_jan: bool = False
) -> None:
    """要確認候補を人が承認する。listing を bottle に紐付け、他候補は superseded。

    ``register_jan=True`` の場合、listing の有効な JAN をそのボトルの識別子として登録する
    (出典は手動確認)。"""
    cand = conn.execute("SELECT * FROM match_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if cand is None:
        raise KeyError(candidate_id)
    if cand["status"] != "pending":
        raise ValueError(f"candidate {candidate_id} is {cand['status']}, not pending")
    now = utcnow()
    with conn:
        conn.execute(
            "UPDATE match_candidates SET status = 'accepted', decided_at = ?, decided_by = ? WHERE id = ?",
            (now, decided_by, candidate_id),
        )
        conn.execute(
            "UPDATE match_candidates SET status = 'superseded', decided_at = ?, decided_by = ? "
            "WHERE listing_id = ? AND id != ? AND status = 'pending'",
            (now, decided_by, cand["listing_id"], candidate_id),
        )
        conn.execute(
            "UPDATE listings SET bottle_id = ?, match_status = 'matched', match_method = 'manual', "
            "match_score = ? WHERE id = ?",
            (cand["bottle_id"], cand["score"], cand["listing_id"]),
        )
        if register_jan:
            listing = conn.execute(
                "SELECT jan_raw FROM listings WHERE id = ?", (cand["listing_id"],)
            ).fetchone()
            gtin = parse_gtin(listing["jan_raw"])
            if gtin:
                cur = conn.execute(
                    "INSERT INTO sources (kind, title, retrieved_at, note) VALUES "
                    "('manual_entry', ?, ?, ?)",
                    (f"照合レビューで確認 (listing {cand['listing_id']})", now, f"by {decided_by}"),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO product_identifiers "
                    "(bottle_id, id_type, value, source_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (cand["bottle_id"], gtin.id_type, gtin.value, cur.lastrowid, now),
                )


def reject_listing(conn: sqlite3.Connection, listing_id: int, decided_by: str) -> None:
    """全候補を却下し listing を 'rejected' にする (カタログ外商品など)。"""
    now = utcnow()
    with conn:
        conn.execute(
            "UPDATE match_candidates SET status = 'rejected', decided_at = ?, decided_by = ? "
            "WHERE listing_id = ? AND status = 'pending'",
            (now, decided_by, listing_id),
        )
        conn.execute(
            "UPDATE listings SET bottle_id = NULL, match_status = 'rejected', match_method = 'manual', "
            "match_score = NULL WHERE id = ?",
            (listing_id,),
        )
