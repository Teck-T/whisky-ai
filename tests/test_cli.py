from __future__ import annotations

from pathlib import Path

import pytest

from whisky_ai.cli import main


def test_cli_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dbp = str(tmp_path / "w.sqlite3")
    assert main(["--db", dbp, "init-db"]) == 0
    assert main(["--db", dbp, "seed-sample"]) == 0
    assert main(["--db", dbp, "seed-sample"]) == 1  # 二重投入は拒否
    assert main(["--db", dbp, "match"]) == 0
    assert '"matched"' in capsys.readouterr().out
    assert main(["--db", dbp, "review", "list"]) == 0
    assert main(["--db", dbp, "stats"]) == 0
    out = capsys.readouterr().out
    assert "listings.matched" in out


def test_cli_collect_mock(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                          monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("RAKUTEN_APPLICATION_ID", "RAKUTEN_ACCESS_KEY", "YAHOO_CLIENT_ID"):
        monkeypatch.delenv(k, raising=False)
    dbp = str(tmp_path / "w.sqlite3")
    assert main(["--db", dbp, "seed-sample"]) == 0
    assert main(["--db", dbp, "collect", "rakuten", "--keyword", "Aokawa"]) == 0
    captured = capsys.readouterr()
    assert "[mock]" in captured.err and '"matched": 2' in captured.out
    assert main(["--db", dbp, "collect", "rakuten", "--jan", "2900000000049"]) == 2  # 楽天は JAN 検索不可
    assert main(["--db", dbp, "collect", "yahoo", "--jan", "2900000000049"]) == 0
    assert main(["--db", dbp, "prices", "1"]) == 0
    assert main(["--db", dbp, "purge-raw"]) == 0
