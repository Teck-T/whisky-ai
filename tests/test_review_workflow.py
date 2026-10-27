"""照合結果の永続化と要確認ワークフロー。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from whisky_ai import repository as repo
from whisky_ai.matching import (
    Catalog,
    Matcher,
    accept_candidate,
    reject_listing,
    resolve_listing,
    resolve_unprocessed,
)
from whisky_ai.normalize import make_gtin13
from whisky_ai.sample_data import SampleCatalog


def _listing(conn: sqlite3.Connection, title: str, jan: str | None = None, ext: str = "x1") -> int:
    with conn:
        return repo.upsert_listing(conn, repo.ListingData("mock", ext, title, jan_raw=jan))


def _status(conn: sqlite3.Connection, lid: int) -> sqlite3.Row:
    row: sqlite3.Row = conn.execute("SELECT * FROM listings WHERE id = ?", (lid,)).fetchone()
    return row


def test_uncertain_match_is_saved_as_pending_not_merged(
    conn: sqlite3.Connection, matcher: Matcher
) -> None:
    lid = _listing(conn, "Mistral Peak Original 700ml")
    resolve_listing(conn, lid, matcher)
    row = _status(conn, lid)
    assert row["match_status"] == "pending_review"
    assert row["bottle_id"] is None
    cands = conn.execute(
        "SELECT * FROM match_candidates WHERE listing_id = ? AND status = 'pending'", (lid,)
    ).fetchall()
    assert len(cands) == 2
    reasons = json.loads(cands[0]["reasons"])
    assert "ambiguous_multiple_candidates" in reasons["listing"]
    assert reasons["spec"]["volume_ml"] == 700


def test_accept_candidate_links_listing_and_supersedes_others(
    conn: sqlite3.Connection, matcher: Matcher, catalog: SampleCatalog
) -> None:
    lid = _listing(conn, "Mistral Peak Original 700ml")
    resolve_listing(conn, lid, matcher)
    target = catalog.bottle_ids["Mistral Peak Original 700ml 43%"]
    cid = conn.execute(
        "SELECT id FROM match_candidates WHERE listing_id = ? AND bottle_id = ?", (lid, target)
    ).fetchone()[0]
    accept_candidate(conn, cid, "tester")
    row = _status(conn, lid)
    assert (row["match_status"], row["bottle_id"], row["match_method"]) == ("matched", target, "manual")
    statuses = {r[0] for r in conn.execute(
        "SELECT status FROM match_candidates WHERE listing_id = ?", (lid,))}
    assert statuses == {"accepted", "superseded"}
    with pytest.raises(ValueError):
        accept_candidate(conn, cid, "tester")


def test_manual_decision_not_overwritten_by_rerun(conn: sqlite3.Connection, matcher: Matcher) -> None:
    lid = _listing(conn, "Mistral Peak Original 700ml")
    resolve_listing(conn, lid, matcher)
    cid = conn.execute("SELECT id FROM match_candidates WHERE listing_id = ?", (lid,)).fetchone()[0]
    accept_candidate(conn, cid, "tester")
    assert resolve_listing(conn, lid, matcher) is None
    assert _status(conn, lid)["match_method"] == "manual"


def test_reject_listing(conn: sqlite3.Connection, matcher: Matcher) -> None:
    lid = _listing(conn, "Mistral Peak Original 700ml")
    resolve_listing(conn, lid, matcher)
    reject_listing(conn, lid, "tester")
    assert _status(conn, lid)["match_status"] == "rejected"
    assert resolve_listing(conn, lid, matcher) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM match_candidates WHERE listing_id = ? AND status = 'pending'", (lid,)
    ).fetchone()[0] == 0


def test_accept_with_register_jan_enables_future_auto_match(
    conn: sqlite3.Connection, matcher: Matcher, catalog: SampleCatalog
) -> None:
    new_jan = make_gtin13("298888888888")  # カタログ未登録の JAN (例: リニューアル後の新 JAN)
    lid = _listing(conn, "Aokawa 12年 700ml", new_jan)
    resolve_listing(conn, lid, matcher)
    assert _status(conn, lid)["match_status"] == "pending_review"
    target = catalog.bottle_ids["Aokawa 12 Years 700ml"]
    cid = conn.execute(
        "SELECT id FROM match_candidates WHERE listing_id = ? AND bottle_id = ?", (lid, target)
    ).fetchone()[0]
    accept_candidate(conn, cid, "tester", register_jan=True)
    src = conn.execute(
        "SELECT s.kind FROM product_identifiers pi JOIN sources s ON s.id = pi.source_id "
        "WHERE pi.value = ?", (new_jan,)
    ).fetchone()
    assert src["kind"] == "manual_entry"

    lid2 = _listing(conn, "青川12年", new_jan, ext="x2")  # 容量なしでも JAN で確定
    res = resolve_listing(conn, lid2, Matcher(Catalog.load(conn)))
    assert res is not None and res.method == "jan"
    assert _status(conn, lid2)["bottle_id"] == target


def test_title_change_resets_match(conn: sqlite3.Connection, matcher: Matcher) -> None:
    lid = _listing(conn, "Aokawa 12年 700ml")
    resolve_listing(conn, lid, matcher)
    assert _status(conn, lid)["match_status"] == "matched"
    assert _listing(conn, "Aokawa 18年 700ml") == lid
    assert _status(conn, lid)["match_status"] == "unprocessed"
    counts = resolve_unprocessed(conn, matcher)
    assert counts == {"matched": 1}
    name = conn.execute(
        "SELECT b.display_name FROM listings l JOIN bottles b ON b.id = l.bottle_id WHERE l.id = ?",
        (lid,),
    ).fetchone()[0]
    assert name == "Aokawa 18 Years 700ml"


def test_rerun_replaces_pending_candidates(conn: sqlite3.Connection, matcher: Matcher) -> None:
    lid = _listing(conn, "Mistral Peak Original 700ml")
    resolve_listing(conn, lid, matcher)
    resolve_listing(conn, lid, matcher, force=True)
    assert conn.execute(
        "SELECT COUNT(*) FROM match_candidates WHERE listing_id = ? AND status = 'pending'", (lid,)
    ).fetchone()[0] == 2
