"""DB 接続 (SQLite / PostgreSQL) とマイグレーション。

マイグレーションは ``migrations/NNNN_name.sql`` を番号順に一度だけ適用する。
適用済みの版は ``schema_migrations`` に記録する。PostgreSQL には型名を変換して適用する (``pg.py``)。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import cast

DEFAULT_DB_ENV = "WHISKY_AI_DB"
DEFAULT_DB_PATH = Path("data/whisky_ai.sqlite3")

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def utcnow() -> str:
    """UTC の ISO8601 文字列 (秒精度, 'Z' 終端)。全テーブル共通の日時表現。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_postgres_url(target: str | Path) -> bool:
    return str(target).startswith(("postgresql://", "postgres://"))


def resolve_target(path: str | Path | None = None) -> str:
    """接続先 (SQLite のファイルパス / ``:memory:`` / PostgreSQL の URL)。

    引数 → 環境変数 ``WHISKY_AI_DB`` → 既定のファイルの順に決める。
    URL を ``Path`` に通すと ``//`` が潰れるため、文字列のまま扱う。
    """
    if path is not None:
        return str(path)
    return os.environ.get(DEFAULT_DB_ENV) or str(DEFAULT_DB_PATH)


def resolve_db_path(path: str | Path | None = None) -> Path:
    return Path(resolve_target(path))


def lock_path(path: str | Path | None = None) -> Path:
    """定期収集の多重起動防止に使うロックファイル。PostgreSQL では URL のハッシュから決める。"""
    target = resolve_target(path)
    if is_postgres_url(target):
        digest = hashlib.sha256(target.encode()).hexdigest()[:12]
        return DEFAULT_DB_PATH.parent / f"pg-{digest}.lock"
    return Path(target).with_suffix(".lock")


def is_postgres(conn: sqlite3.Connection) -> bool:
    return type(conn).__module__ == "whisky_ai.pg"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """接続を返す。SQLite は外部キー有効・Row ファクトリ付き。``":memory:"`` も可。

    ``postgresql://`` で始まる URL なら PostgreSQL に接続する (``pip install -e ".[postgres]"`` が必要)。
    戻り値の型は ``sqlite3.Connection`` としているが、PostgreSQL の場合は互換アダプタ
    (``whisky_ai.pg.PgConnection``) で、このコードベースが使う機能だけを実装している。
    """
    target = resolve_target(path)
    if is_postgres_url(target):
        from whisky_ai.pg import PgConnection

        return cast(sqlite3.Connection, PgConnection(target))
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
        if is_postgres(conn):
            from whisky_ai.pg import migration_to_postgres

            # PostgreSQL は DDL もトランザクションに入るので、適用と記録を 1 トランザクションにする
            with conn:
                conn.executescript(migration_to_postgres(mig.sql, first=mig.version == 1))
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (mig.version, mig.name, utcnow()),
                )
            applied.append(mig.name)
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
