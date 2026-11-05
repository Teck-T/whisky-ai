"""PostgreSQL 接続アダプタ。

このコードベースが使っている ``sqlite3.Connection`` の機能だけを、psycopg 3 の上に実装する。
アプリのコード (repository / matching / ...) は SQLite と同じ書き方のまま PostgreSQL で動く。

sqlite3 との対応:

* プレースホルダ ``?`` → ``%s`` に変換する (文字列リテラル内の ``?`` は変換しない)。
* 行は ``PgRow`` で、``sqlite3.Row`` と同じく列名でも位置でも参照できる。
* ``lastrowid``: INSERT に ``RETURNING *`` を付け、戻った行の ``id`` を入れる。
* ``with conn:`` は sqlite3 と同じく、正常終了でコミット、例外でロールバックする (接続は閉じない)。
* 例外は ``sqlite3.IntegrityError`` などに変換する。呼び出し側の except 節をそのまま使うため。
* **違い**: PostgreSQL ではエラーが起きるとトランザクション全体が無効になる。そのため ``with`` の外で
  エラーが起きたときは自動でロールバックする (コミット前の変更は失われる)。
  SQLite は失敗した 1 文だけを取り消す。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from types import TracebackType
from typing import Any

import psycopg
from psycopg import pq


class PgRow(tuple[Any, ...]):
    """``sqlite3.Row`` 互換: ``row["col"]`` と ``row[0]`` の両方で参照できる。"""

    _index: dict[str, int]

    def __new__(cls, values: Sequence[Any], index: dict[str, int]) -> PgRow:
        row = super().__new__(cls, values)
        row._index = index
        return row

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            return tuple.__getitem__(self, self._index[key])
        return tuple.__getitem__(self, key)

    def keys(self) -> list[str]:
        return list(self._index)


def _row_factory(cursor: psycopg.Cursor[Any]) -> Any:
    names = [c.name for c in cursor.description or ()]
    index = {n: i for i, n in enumerate(names)}
    return lambda values: PgRow(values, index)


def translate(sql: str) -> str:
    """sqlite3 形式の SQL を psycopg 形式にする。

    文字列リテラル外の ``?`` を ``%s`` に、``%`` を ``%%`` にする (psycopg はパラメータ付きの
    クエリ全体で ``%`` を解釈するため、リテラル内の ``%`` もエスケープが必要)。
    """
    out = []
    in_str = False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
            out.append(ch)
        elif ch == "%":
            out.append("%%")
        elif ch == "?" and not in_str:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


_INSERT_RE = re.compile(r"^\s*INSERT\s", re.IGNORECASE)
_RETURNING_RE = re.compile(r"\bRETURNING\b", re.IGNORECASE)

_ERROR_MAP: list[tuple[type[psycopg.Error], type[sqlite3.Error]]] = [
    (psycopg.errors.IntegrityError, sqlite3.IntegrityError),
    (psycopg.errors.DataError, sqlite3.DataError),
    (psycopg.errors.ProgrammingError, sqlite3.ProgrammingError),
]


def _translate_error(e: psycopg.Error) -> sqlite3.Error:
    for pg_type, sqlite_type in _ERROR_MAP:
        if isinstance(e, pg_type):
            return sqlite_type(str(e))
    return sqlite3.OperationalError(str(e))


class PgCursor:
    def __init__(self, cur: psycopg.Cursor[Any], lastrowid: int | None) -> None:
        self._cur = cur
        self.lastrowid = lastrowid
        self.rowcount = cur.rowcount

    def fetchone(self) -> Any:
        return self._cur.fetchone() if self._cur.description else None

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall() if self._cur.description else []

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())


class PgConnection:
    """``sqlite3.Connection`` のうち、このコードベースが使う部分の互換実装。"""

    def __init__(self, url: str, *, schema: str | None = None) -> None:
        options = f"-c search_path={schema}" if schema else None
        kwargs: dict[str, Any] = {"options": options} if options else {}
        self.raw: psycopg.Connection[Any] = psycopg.connect(url, row_factory=_row_factory, **kwargs)
        self._depth = 0  # with ブロックの入れ子の深さ

    # --- sqlite3.Connection 互換 API ---

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> PgCursor:
        is_insert = bool(_INSERT_RE.match(sql)) and not _RETURNING_RE.search(sql)
        query = translate(sql) + (" RETURNING *" if is_insert else "")
        try:
            cur = self.raw.execute(query, tuple(parameters))
        except psycopg.Error as e:
            if self._depth == 0:
                self.raw.rollback()
            raise _translate_error(e) from e
        lastrowid = None
        if is_insert and cur.description:
            row = cur.fetchone()
            if row is not None and "id" in row._index:  # tuple の in は値を探すため列名で判定
                lastrowid = row["id"]
        return PgCursor(cur, lastrowid)

    def executemany(self, sql: str, seq_of_parameters: Sequence[Sequence[Any]]) -> None:
        try:
            with self.raw.cursor() as cur:
                cur.executemany(translate(sql), [tuple(p) for p in seq_of_parameters])
        except psycopg.Error as e:
            if self._depth == 0:
                self.raw.rollback()
            raise _translate_error(e) from e

    def executescript(self, script: str) -> None:
        """パラメータなしの複数文をまとめて実行する (マイグレーション用)。"""
        try:
            self.raw.execute(script.encode("utf-8"))
        except psycopg.Error as e:
            if self._depth == 0:
                self.raw.rollback()
            raise _translate_error(e) from e

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    @property
    def in_transaction(self) -> bool:
        return self.raw.info.transaction_status != pq.TransactionStatus.IDLE

    def __enter__(self) -> PgConnection:
        self._depth += 1
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        # sqlite3 と同じく、入れ子でも出るたびにコミット / ロールバックする
        self._depth -= 1
        if exc_type is None:
            self.raw.commit()
        else:
            self.raw.rollback()


# ===== マイグレーションの変換 =====

_PG_PRELUDE = """
-- SQLite の json_valid() 相当 (CHECK 制約で使う)
CREATE OR REPLACE FUNCTION json_valid(t text) RETURNS boolean
    LANGUAGE plpgsql IMMUTABLE AS $fn$
BEGIN
    PERFORM t::jsonb;
    RETURN true;
EXCEPTION WHEN others THEN
    RETURN false;
END
$fn$;
"""

_TYPE_RULES = [
    (re.compile(r"\bINTEGER PRIMARY KEY\b"), "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"),
    (re.compile(r"\bINTEGER\b"), "BIGINT"),
    (re.compile(r"\bREAL\b"), "DOUBLE PRECISION"),
]


def migration_to_postgres(sql: str, *, first: bool) -> str:
    """SQLite 用マイグレーションを PostgreSQL 用に変換する。

    マイグレーションは SQLite 版だけを正として管理し、型名だけを機械的に置き換える。
    そのため SQLite 版では、両方で通る構文だけを使うこと (docs/design.md §8)。
    """
    for pattern, repl in _TYPE_RULES:
        sql = pattern.sub(repl, sql)
    return (_PG_PRELUDE + sql) if first else sql
