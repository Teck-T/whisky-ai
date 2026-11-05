"""DB 間のデータ移行 (SQLite → PostgreSQL、またはその逆)。

移行先は ``migrate`` 済みで空であること。ID はそのまま引き継ぎ、PostgreSQL では
IDENTITY 列の採番を最大 ID の次から始まるように合わせる。
"""

from __future__ import annotations

import re
import sqlite3

from whisky_ai import db

_CREATE_RE = re.compile(r"^CREATE TABLE (\w+)", re.MULTILINE)


def table_order() -> list[str]:
    """マイグレーションで作られる順 (= 外部キーの依存順) のテーブル名。"""
    return [name for mig in db.load_migrations() for name in _CREATE_RE.findall(mig.sql)]


def copy_database(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict[str, int]:
    src_version, dst_version = db.current_version(src), db.current_version(dst)
    if src_version != dst_version:
        raise ValueError(f"schema version differs: source {src_version}, destination {dst_version}"
                         " (run migrate on both first)")
    tables = table_order()
    nonempty = [t for t in tables if dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]]
    if nonempty:
        raise ValueError(f"destination is not empty: {', '.join(nonempty)}")
    copied: dict[str, int] = {}
    with dst:
        for table in tables:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            copied[table] = len(rows)
            if not rows:
                continue
            cols = list(rows[0].keys())
            sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})")
            dst.executemany(sql, [tuple(r) for r in rows])
        if db.is_postgres(dst):
            for table in tables:
                if copied[table] and "id" in _columns(src, table):
                    dst.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), MAX(id)) FROM {table}"
                    )
    return copied


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    row = conn.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
    return list(row.keys()) if row is not None else []
