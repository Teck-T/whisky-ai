"""価格推移と値下がり通知のテスト。時刻はすべて固定値を渡す。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from whisky_ai import alerts
from whisky_ai import repository as repo
from whisky_ai.price_history import current_best, daily_best, history, observations, reference_price
from whisky_ai.sample_data import SampleCatalog

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)  # JST 12:00


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def setup(conn: sqlite3.Connection, catalog: SampleCatalog) -> dict[str, int]:
    bottle = catalog.bottle_ids["Aokawa 12 Years 700ml"]
    with conn:
        src = repo.add_source(conn, "api", "test api")
        ids = {"bottle": bottle, "src": src}
        for name in ("a", "b"):
            shop = repo.upsert_shop(conn, "rakuten", f"shop-{name}", f"店{name.upper()}")
            lid = repo.upsert_listing(conn, repo.ListingData(
                "rakuten", f"shop-{name}:1", "Aokawa 12年 700ml", shop_id=shop,
                item_url=f"https://example.invalid/{name}"))
            conn.execute("UPDATE listings SET bottle_id = ?, match_status = 'matched',"
                         " match_method = 'manual' WHERE id = ?", (bottle, lid))
            ids[name] = lid
    return ids


def observe(conn: sqlite3.Connection, s: dict[str, int], listing: str, at: datetime, price: int | None,
            *, in_stock: bool | None = True, tax_included: bool = True) -> None:
    with conn:
        repo.record_price_observation(conn, repo.PriceObservation(
            s[listing], price if price is not None else 1, s["src"], observed_at=ts(at),
            price_tax_included=tax_included if price is not None else False, in_stock=in_stock))


def seed_month(conn: sqlite3.Connection, s: dict[str, int], price: int = 6000) -> None:
    """過去 30 日、毎日 1 回 A 店で ``price`` 円。"""
    for d in range(1, 31):
        observe(conn, s, "a", NOW - timedelta(days=d), price)


# ---------- 価格推移 ----------

def test_daily_best_uses_jst_and_min_per_day(conn: sqlite3.Connection, setup: dict[str, int]) -> None:
    # UTC 11/4 16:00 = JST 11/5 01:00 → 11/5 として数える
    observe(conn, setup, "a", datetime(2026, 11, 4, 16, 0, tzinfo=UTC), 6000)
    observe(conn, setup, "b", datetime(2026, 11, 5, 2, 0, tzinfo=UTC), 5800)
    observe(conn, setup, "a", datetime(2026, 11, 4, 14, 0, tzinfo=UTC), 6100)  # JST 11/4 23:00
    series = daily_best(observations(conn, setup["bottle"]))
    assert [(d.day, d.price_jpy, d.listings, d.shop_name) for d in series] == [
        (date(2026, 11, 4), 6100, 1, "店A"), (date(2026, 11, 5), 5800, 2, "店B")]


def test_history_excludes_out_of_stock_unknown_tax_and_unmatched(
    conn: sqlite3.Connection, setup: dict[str, int]
) -> None:
    observe(conn, setup, "a", NOW - timedelta(hours=1), 4000, in_stock=False)
    observe(conn, setup, "a", NOW - timedelta(hours=2), 4500, tax_included=False)  # 税込不明
    observe(conn, setup, "b", NOW - timedelta(hours=3), 6000)
    with conn:
        conn.execute("UPDATE listings SET bottle_id = NULL, match_status = 'pending_review',"
                     " match_method = NULL WHERE id = ?", (setup["a"],))
        observe(conn, setup, "a", NOW - timedelta(hours=4), 3000)
    assert [d.price_jpy for d in history(conn, setup["bottle"], days=7, now=NOW)] == [6000]


def test_current_best_uses_latest_and_fresh_observations(
    conn: sqlite3.Connection, setup: dict[str, int]
) -> None:
    observe(conn, setup, "a", NOW - timedelta(hours=10), 5000)
    observe(conn, setup, "a", NOW - timedelta(hours=1), 5500)  # A の最新
    observe(conn, setup, "b", NOW - timedelta(hours=100), 4000)  # 古すぎる
    best = current_best(conn, setup["bottle"], now=NOW)
    assert best is not None and (best.price_jpy, best.listing_id) == (5500, setup["a"])
    observe(conn, setup, "a", NOW - timedelta(minutes=5), 5200, in_stock=False)  # 最新が在庫切れ
    assert current_best(conn, setup["bottle"], now=NOW) is None


def test_reference_price_is_median_and_needs_history() -> None:
    from whisky_ai.price_history import DailyBest

    days = [DailyBest(date(2026, 11, d), p, 1, None, "rakuten") for d, p in
            [(1, 6000), (2, 6200), (3, 3000), (4, 6100)]]
    assert reference_price(days, date(2026, 11, 5), 30) == 6050  # 特売 3,000 円に引っ張られない
    assert reference_price(days, date(2026, 11, 3), 30) is None  # 2 日分しかない
    assert reference_price(days, date(2026, 11, 5), 2) is None  # 11/3〜4 の 2 日分だけ


# ---------- 通知ルール ----------

def test_add_rule_validation(conn: sqlite3.Connection, setup: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        alerts.add_rule(conn, setup["bottle"])
    with pytest.raises(ValueError):
        alerts.add_rule(conn, setup["bottle"], target_price_jpy=1, drop_pct=5)
    with pytest.raises(KeyError):
        alerts.add_rule(conn, 999999, target_price_jpy=5000)
    with pytest.raises(sqlite3.IntegrityError):
        alerts.add_rule(conn, setup["bottle"], drop_pct=150)
    with pytest.raises(KeyError):
        alerts.remove_rule(conn, 999)


def test_target_price_fires_once_then_rearms(conn: sqlite3.Connection, setup: dict[str, int]) -> None:
    alerts.add_rule(conn, setup["bottle"], target_price_jpy=5500)
    observe(conn, setup, "a", NOW - timedelta(hours=1), 5800)
    assert alerts.evaluate(conn, now=NOW) == []

    t1 = NOW + timedelta(hours=12)
    observe(conn, setup, "b", t1 - timedelta(hours=1), 5400)
    [ev] = alerts.evaluate(conn, now=t1)
    assert ev.price_jpy == 5400 and ev.reference_price_jpy == 5500 and "店B" in ev.message
    assert alerts.evaluate(conn, now=t1) == []  # 同じ価格では再通知しない

    t2 = t1 + timedelta(hours=12)
    observe(conn, setup, "b", t2 - timedelta(hours=1), 5300)  # さらに下がった → 通知
    assert [e.price_jpy for e in alerts.evaluate(conn, now=t2)] == [5300]

    t3 = t2 + timedelta(hours=12)
    observe(conn, setup, "b", t3 - timedelta(hours=1), 5900)  # 条件外 → 再武装
    observe(conn, setup, "a", t3 - timedelta(hours=1), 5900)
    assert alerts.evaluate(conn, now=t3) == []
    t4 = t3 + timedelta(hours=12)
    observe(conn, setup, "b", t4 - timedelta(hours=1), 5450)  # 前回 5,300 より高いが再武装後なので通知
    assert [e.price_jpy for e in alerts.evaluate(conn, now=t4)] == [5450]
    assert conn.execute("SELECT COUNT(*) FROM price_alert_events").fetchone()[0] == 3


def test_drop_pct_against_median(conn: sqlite3.Connection, setup: dict[str, int]) -> None:
    seed_month(conn, setup, 6000)
    alerts.add_rule(conn, setup["bottle"], drop_pct=10)
    observe(conn, setup, "b", NOW - timedelta(hours=1), 5500)  # 8.3% 安 → まだ
    assert alerts.evaluate(conn, now=NOW) == []
    observe(conn, setup, "b", NOW - timedelta(minutes=30), 5300)  # 11.7% 安 → 通知
    [ev] = alerts.evaluate(conn, now=NOW)
    assert ev.reference_price_jpy == 6000 and "12% 安い" in ev.message


def test_drop_pct_without_enough_history_is_silent(
    conn: sqlite3.Connection, setup: dict[str, int]
) -> None:
    alerts.add_rule(conn, setup["bottle"], drop_pct=5)
    observe(conn, setup, "a", NOW - timedelta(days=1), 6000)
    observe(conn, setup, "a", NOW - timedelta(hours=1), 3000)
    assert alerts.evaluate(conn, now=NOW) == []


def test_disabled_and_no_data(conn: sqlite3.Connection, setup: dict[str, int]) -> None:
    rid = alerts.add_rule(conn, setup["bottle"], target_price_jpy=99999)
    assert alerts.evaluate(conn, now=NOW) == []  # 価格データなし
    with conn:
        conn.execute("UPDATE price_alert_rules SET enabled = 0 WHERE id = ?", (rid,))
    observe(conn, setup, "a", NOW - timedelta(hours=1), 5000)
    assert alerts.evaluate(conn, now=NOW) == []


# ---------- 通知の送信 ----------

def _event(conn: sqlite3.Connection, setup: dict[str, int]) -> list[alerts.AlertEvent]:
    alerts.add_rule(conn, setup["bottle"], target_price_jpy=9999)
    observe(conn, setup, "a", NOW - timedelta(hours=1), 5000)
    return alerts.evaluate(conn, now=NOW)


def test_notify_records_channels_and_continues_on_failure(
    conn: sqlite3.Connection, setup: dict[str, int]
) -> None:
    events = _event(conn, setup)
    got: list[str] = []

    def boom(e: alerts.AlertEvent) -> None:
        raise RuntimeError("down")

    errors = alerts.notify(conn, events, ["stdout", "webhook"],
                           senders={"stdout": lambda e: got.append(e.message), "webhook": boom,
                                    "macos": lambda e: None})
    assert len(got) == 1 and errors == ["webhook: RuntimeError: down"]
    assert conn.execute("SELECT notified_channels FROM price_alert_events").fetchone()[0] == "stdout"


def test_webhook_payload_and_missing_url(conn: sqlite3.Connection, setup: dict[str, int],
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    [ev] = _event(conn, setup)
    posted: list[tuple[str, dict[str, object]]] = []
    alerts.send_webhook(ev, url="https://hooks.example.invalid/x",
                        post=lambda url, body: posted.append((url, json.loads(body))))
    url, body = posted[0]
    assert url == "https://hooks.example.invalid/x"
    assert body["text"] == body["content"] and "5,000 円" in str(body["text"])
    assert "https://example.invalid/a" in str(body["text"])
    monkeypatch.delenv(alerts.WEBHOOK_ENV, raising=False)
    with pytest.raises(RuntimeError):
        alerts.send_webhook(ev)


def test_macos_notification_escapes_and_skips_elsewhere(
    conn: sqlite3.Connection, setup: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    [ev] = _event(conn, setup)
    calls: list[list[str]] = []
    monkeypatch.setattr(alerts.platform, "system", lambda: "Linux")
    alerts.send_macos(ev, run=lambda cmd, **_: calls.append(cmd))
    assert calls == []
    monkeypatch.setattr(alerts.platform, "system", lambda: "Darwin")
    tricky = alerts.AlertEvent(**{**ev.__dict__, "message": 'say "hi" \\ done'})
    alerts.send_macos(tricky, run=lambda cmd, **_: calls.append(cmd))
    assert calls[0][:2] == ["osascript", "-e"]
    assert 'display notification "say \\"hi\\" \\\\ done"' in calls[0][2]


def test_default_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts.platform, "system", lambda: "Darwin")
    monkeypatch.setenv(alerts.WEBHOOK_ENV, "https://hooks.example.invalid/x")
    assert alerts.default_channels() == ["stdout", "macos", "webhook"]
    monkeypatch.setattr(alerts.platform, "system", lambda: "Linux")
    monkeypatch.delenv(alerts.WEBHOOK_ENV)
    assert alerts.default_channels() == ["stdout"]
