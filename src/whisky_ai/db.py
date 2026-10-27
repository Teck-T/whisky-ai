"""SQLite 接続とマイグレーション。

マイグレーションは ``migrations/NNNN_name.sql`` を番号順に一度だけ適用する。
適用済みの版は ``schema_migrations`` に記録する。
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

DEFAULT_DB_ENV = "WHISKY_AI_DB"
DEFAULT_DB_PATH = Path("data/whisky_ai.sqlite3")

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def utcnow() -> str:
    """UTC の ISO8601 文字列 (秒精度, 'Z' 終端)。全テーブル共通の日時表現。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_db_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(DEFAULT_DB_ENV)
    return Path(env) if env else DEFAULT_DB_PATH


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """外部キー有効・Row ファクトリ付きの接続を返す。``":memory:"`` も可。"""
    target = str(path) if path == ":memory:" else str(resolve_db_path(path))
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    pkg = resources.files("whisky_ai") / "migrations"
    found: list[Migration] = []
    for entry in pkg.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append(Migration(int(m.group(1)), entry.name, entry.read_text("utf-8")))
    found.sort(key=lambda mig: mig.version)
    versions = [mig.version for mig in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"duplicate migration versions: {versions}")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """未適用のマイグレーションを適用し、適用したファイル名を返す。"""
    applied: list[str] = []
    version = current_version(conn)
    for mig in load_migrations():
        if mig.version <= version:
            continue
        # executescript は暗黙 COMMIT するため、明示トランザクションで包む
        try:
            conn.executescript(f"BEGIN;\n{mig.sql}\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            raise
        with conn:
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (mig.version, mig.name, utcnow()),
            )
        applied.append(mig.name)
    return applied
