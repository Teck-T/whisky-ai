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


# ---------- JSON Schema / JSONL 取り込み ----------

from whisky_ai.flavor import AXES, PHASES, import_jsonl, json_schema  # noqa: E402


def test_json_schema_matches_validator_constraints() -> None:
    schema = json_schema()
    assert schema["properties"]["schema_version"] == {"const": "1"}
    assert set(schema["properties"]["axes"]["properties"]) == set(AXES)
    assert schema["properties"]["axes"]["additionalProperties"] is False
    for phase in PHASES:
        item = schema["properties"][phase]["items"]
        assert item["required"] == ["descriptor"]
        assert item["properties"]["intensity"] == {"type": "number", "minimum": 0, "maximum": 1}
    json.dumps(schema)  # シリアライズ可能


def _line(target: dict[str, int], payload: object, **extra: object) -> str:
    return json.dumps({"target": target, "model_name": "qwen-local", "prompt_version": "p1",
                       "payload": payload, **extra}, ensure_ascii=False)


def test_import_jsonl(conn: sqlite3.Connection, catalog: SampleCatalog) -> None:
    rel = catalog.release_ids["Aokawa 12 Years"]
    bottle = catalog.bottle_ids["Aokawa 12 Years 700ml"]
    review = {"body": "甘いバニラと青リンゴ。", "source_title": "個人のテイスティングノート", "lang": "ja"}
    lines = [
        _line({"release_id": rel}, VALID),
        _line({"bottle_id": bottle}, VALID, review=review),
        _line({"release_id": rel}, {"schema_version": "1", "axes": {"sweetness": 2}}),  # 不正 → invalid
        "",
        "{broken json",
        _line({"release_id": 999999}, VALID),
        _line({}, VALID),
        _line({"release_id": rel}, VALID, review={"body": "x", "source_kind": "no-such-kind"}),
    ]
    stats = import_jsonl(conn, lines)
    assert (stats["valid"], stats["invalid"], stats["skipped"]) == (2, 1, 4)
    assert any("line 5" in e for e in stats["errors"])
    row = conn.execute(
        "SELECT r.body, r.bottle_id, s.kind FROM reviews r JOIN sources s ON s.id = r.source_id"
    ).fetchone()
    assert tuple(row) == ("甘いバニラと青リンゴ。", bottle, "manual_entry")
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1  # 失敗行のレビューは残らない
    assert conn.execute("SELECT review_id FROM flavor_extractions WHERE bottle_id = ?",
                        (bottle,)).fetchone()[0] is not None
