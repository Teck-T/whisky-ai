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


def test_cli_watch_flow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dbp = str(tmp_path / "w.sqlite3")
    assert main(["--db", dbp, "seed-sample"]) == 0
    assert main(["--db", dbp, "watch", "add", "--bottle-id", "1"]) == 0
    assert main(["--db", dbp, "watch", "add", "--keyword", "Mistral Peak", "--source", "rakuten"]) == 0
    assert main(["--db", dbp, "watch", "add"]) == 2
    assert main(["--db", dbp, "watch", "run", "--mock"]) == 0
    assert main(["--db", dbp, "watch", "disable", "1"]) == 0
    capsys.readouterr()
    assert main(["--db", dbp, "watch", "list"]) == 0
    out = capsys.readouterr().out
    assert "[1] off" in out and " ok" in out
    assert (tmp_path / "w.lock").exists()


def test_cli_flavor_and_recommend(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    dbp = str(tmp_path / "w.sqlite3")
    assert main(["--db", dbp, "seed-sample"]) == 0
    capsys.readouterr()
    assert main(["--db", dbp, "flavor", "schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"].startswith("Whisky flavor")
    jsonl = tmp_path / "x.jsonl"
    jsonl.write_text(json.dumps({"target": {"release_id": 1}, "model_name": "m", "prompt_version": "p",
                                 "payload": {"schema_version": "1", "nose": [], "palate": [],
                                             "finish": [], "axes": {"sweetness": 0.9}}}) + "\n",
                     encoding="utf-8")
    assert main(["--db", dbp, "flavor", "import", str(jsonl)]) == 0
    assert '"valid": 1' in capsys.readouterr().out
    assert main(["--db", dbp, "flavor", "show", "1"]) == 0
    assert "sweetness" in capsys.readouterr().out
    assert main(["--db", dbp, "recommend", "--like", "1", "--limit", "3"]) == 0
    assert " 1. " in capsys.readouterr().out
    assert main(["--db", dbp, "recommend", "--taste", "umami=1"]) == 2


def test_cli_price_history_and_alerts(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from whisky_ai import alerts

    monkeypatch.setattr(alerts.platform, "system", lambda: "Linux")
    monkeypatch.delenv(alerts.WEBHOOK_ENV, raising=False)
    dbp = str(tmp_path / "w.sqlite3")
    assert main(["--db", dbp, "seed-sample"]) == 0
    assert main(["--db", dbp, "watch", "add", "--keyword", "Aokawa", "--source", "yahoo"]) == 0
    assert main(["--db", dbp, "alert", "add", "--bottle-id", "4", "--target-price", "7000"]) == 0
    with pytest.raises(SystemExit):  # 条件 (--target-price / --drop-pct) は必須
        main(["--db", dbp, "alert", "add", "--bottle-id", "4"])
    capsys.readouterr()
    assert main(["--db", dbp, "watch", "run", "--mock"]) == 0
    out = capsys.readouterr().out
    assert "値下がり: Aokawa 12 Years 700ml が 6,200 円" in out and '"alerts": 1' in out
    assert main(["--db", dbp, "alert", "check"]) == 0
    assert "0 alert(s)" in capsys.readouterr().out  # 同じ価格では再通知しない
    assert main(["--db", dbp, "alert", "events"]) == 0
    assert "[stdout]" in capsys.readouterr().out
    assert main(["--db", dbp, "price-history", "4"]) == 0
    assert "6,200 円" in capsys.readouterr().out
    assert main(["--db", dbp, "alert", "list"]) == 0
    assert "7,000 円以下" in capsys.readouterr().out
    assert main(["--db", dbp, "alert", "check", "--notify", "fax"]) == 2
    assert main(["--db", dbp, "alert", "remove", "1"]) == 0
