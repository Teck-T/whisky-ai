from __future__ import annotations

import json
import sqlite3

from whisky_ai.flavor import store_extraction, validate_payload
from whisky_ai.sample_data import SampleCatalog

VALID = {
    "schema_version": "1",
    "nose": [{"descriptor": "バニラ", "category": "sweet", "intensity": 0.7}],
    "palate": [{"descriptor": "ドライフルーツ", "category": "fruity"}],
    "finish": [{"descriptor": "ピート", "category": "smoky", "intensity": 0.4}],
    "axes": {"sweetness": 0.6, "smokiness": 0.3},
    "summary": "甘く穏やか",
    "confidence": 0.8,
}


def test_validate_ok() -> None:
    assert validate_payload(VALID) == []


def test_validate_errors() -> None:
    bad = {**VALID, "schema_version": "0", "axes": {"sweetness": 1.5, "unknown": 0.1},
           "nose": [{"descriptor": ""}]}
    errors = validate_payload(bad)
    assert len(errors) == 4


def test_store_valid_expands_notes(conn: sqlite3.Connection, catalog: SampleCatalog) -> None:
    rel = catalog.release_ids["Aokawa 12 Years"]
    eid = store_extraction(conn, VALID, model_name="qwen-local", prompt_version="p1", release_id=rel)
    row = conn.execute("SELECT * FROM flavor_extractions WHERE id = ?", (eid,)).fetchone()
    assert row["is_valid"] == 1 and json.loads(row["payload"])["summary"] == "甘く穏やか"
    notes = conn.execute("SELECT phase, descriptor FROM flavor_notes WHERE extraction_id = ?",
                         (eid,)).fetchall()
    assert {(n[0], n[1]) for n in notes} == {("nose", "バニラ"), ("palate", "ドライフルーツ"),
                                             ("finish", "ピート")}
    axes = dict(conn.execute("SELECT axis, value FROM flavor_axes WHERE extraction_id = ?", (eid,)))
    assert axes == {"sweetness": 0.6, "smokiness": 0.3}


def test_store_invalid_keeps_raw(conn: sqlite3.Connection, catalog: SampleCatalog) -> None:
    rel = catalog.release_ids["Aokawa 12 Years"]
    eid = store_extraction(conn, '{"nose": "not a list"', model_name="qwen-local",
                           prompt_version="p1", release_id=rel)
    row = conn.execute("SELECT * FROM flavor_extractions WHERE id = ?", (eid,)).fetchone()
    assert row["is_valid"] == 0
    assert "_raw" in json.loads(row["payload"])
    assert conn.execute("SELECT COUNT(*) FROM flavor_notes").fetchone()[0] == 0
