"""LLM (ローカル Qwen 等) が出力する香味特徴 JSON の検証と保存。

JSON 原文は常に ``flavor_extractions.payload`` に保存する (不正でも捨てない: プロンプト改善の材料)。
検証に通ったものだけ ``flavor_notes`` / ``flavor_axes`` に展開し、検索・推薦に使う。

スキーマ v1 の例::

    {
      "schema_version": "1",
      "nose":   [{"descriptor": "バニラ", "category": "sweet", "intensity": 0.7}],
      "palate": [{"descriptor": "ドライフルーツ", "category": "fruity"}],
      "finish": [{"descriptor": "ピート", "category": "smoky", "intensity": 0.4}],
      "axes": {"sweetness": 0.6, "smokiness": 0.3},
      "summary": "甘く穏やかなシェリー系",
      "confidence": 0.8
    }
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from whisky_ai.db import utcnow

SCHEMA_VERSION = "1"
PHASES = ("nose", "palate", "finish", "overall")
# 推薦で使う数値軸。未知の軸はエラーにせず警告扱いにしたいが、v1 では固定集合で厳格に扱う。
AXES = frozenset({
    "sweetness", "smokiness", "peatiness", "fruitiness", "floral", "spiciness",
    "woodiness", "maltiness", "sherry", "body", "richness", "finish_length",
})


def validate_payload(payload: Any) -> list[str]:
    """エラーメッセージのリストを返す (空なら妥当)。外部依存なしの最小検証。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    if str(payload.get("schema_version")) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    for phase in PHASES:
        notes = payload.get(phase, [])
        if not isinstance(notes, list):
            errors.append(f"{phase} must be a list")
            continue
        for i, note in enumerate(notes):
            where = f"{phase}[{i}]"
            if not isinstance(note, dict):
                errors.append(f"{where} must be an object")
                continue
            desc = note.get("descriptor")
            if not isinstance(desc, str) or not desc.strip():
                errors.append(f"{where}.descriptor must be a non-empty string")
            cat = note.get("category")
            if cat is not None and not isinstance(cat, str):
                errors.append(f"{where}.category must be a string")
            inten = note.get("intensity")
            if inten is not None and not _unit(inten):
                errors.append(f"{where}.intensity must be a number in [0, 1]")
    axes = payload.get("axes", {})
    if not isinstance(axes, dict):
        errors.append("axes must be an object")
    else:
        for k, v in axes.items():
            if k not in AXES:
                errors.append(f"axes.{k} is not a known axis")
            elif not _unit(v):
                errors.append(f"axes.{k} must be a number in [0, 1]")
    conf = payload.get("confidence")
    if conf is not None and not _unit(conf):
        errors.append("confidence must be a number in [0, 1]")
    return errors


def _unit(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and 0 <= v <= 1


def store_extraction(
    conn: sqlite3.Connection,
    payload: Any,
    *,
    model_name: str,
    prompt_version: str,
    model_revision: str | None = None,
    review_id: int | None = None,
    release_id: int | None = None,
    batch_id: int | None = None,
    bottle_id: int | None = None,
) -> int:
    """抽出結果を保存し extraction id を返す。``payload`` は dict か JSON 文字列。

    JSON として壊れた文字列も ``{"_raw": ...}`` で包んで保存し is_valid=0 にする。
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as e:
            payload, errors = {"_raw": payload}, [f"invalid JSON: {e.msg}"]
        else:
            errors = validate_payload(payload)
    else:
        errors = validate_payload(payload)
    with conn:
        cur = conn.execute(
            """
            INSERT INTO flavor_extractions
                (review_id, release_id, batch_id, bottle_id, model_name, model_revision,
                 prompt_version, schema_version, payload, is_valid, validation_errors, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (review_id, release_id, batch_id, bottle_id, model_name, model_revision,
             prompt_version, str(payload.get("schema_version", "")) if isinstance(payload, dict) else "",
             json.dumps(payload, ensure_ascii=False), int(not errors),
             json.dumps(errors, ensure_ascii=False) if errors else None, utcnow()),
        )
        extraction_id = cur.lastrowid
        assert extraction_id is not None
        if not errors:
            for phase in PHASES:
                for note in payload.get(phase, []):
                    conn.execute(
                        "INSERT INTO flavor_notes (extraction_id, phase, descriptor, category, intensity) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (extraction_id, phase, note["descriptor"].strip(), note.get("category"),
                         note.get("intensity")),
                    )
            for axis, value in payload.get("axes", {}).items():
                conn.execute(
                    "INSERT INTO flavor_axes (extraction_id, axis, value) VALUES (?, ?, ?)",
                    (extraction_id, axis, float(value)),
                )
    return extraction_id
