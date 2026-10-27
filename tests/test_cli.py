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
