"""値下がり通知。

ルールは 2 種類:

* ``target_price``: 現在の最安値 (税込・送料別) が目標価格以下になったら通知
* ``drop_pct``: 現在の最安値が、直近 ``lookback_days`` 日の相場 (日別最安値の中央値) より
  ``drop_pct`` % 以上安くなったら通知

同じ値下がりで何度も通知しないよう、ルールごとに ``armed`` を持つ。
条件を満たして通知したら armed=0。条件を満たさない状態を一度見たら armed=1 に戻る。
armed=0 のままでも、前回通知した価格よりさらに下がったら通知する。
"""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import subprocess
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from whisky_ai.db import utcnow
from whisky_ai.price_history import JST, current_best, history, reference_price

Channel = Literal["stdout", "macos", "webhook"]
WEBHOOK_ENV = "WHISKY_AI_WEBHOOK_URL"


def add_rule(
    conn: sqlite3.Connection, bottle_id: int, *, target_price_jpy: int | None = None,
    drop_pct: float | None = None, lookback_days: int = 30,
) -> int:
    if (target_price_jpy is None) == (drop_pct is None):
        raise ValueError("specify exactly one of target_price_jpy or drop_pct")
    if conn.execute("SELECT 1 FROM bottles WHERE id = ?", (bottle_id,)).fetchone() is None:
        raise KeyError(f"bottle {bottle_id} not found")
    kind = "target_price" if target_price_jpy is not None else "drop_pct"
    with conn:
        cur = conn.execute(
            "INSERT INTO price_alert_rules (bottle_id, kind, target_price_jpy, drop_pct, lookback_days,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (bottle_id, kind, target_price_jpy, drop_pct, lookback_days, utcnow()),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def remove_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    with conn:
        cur = conn.execute("DELETE FROM price_alert_rules WHERE id = ?", (rule_id,))
    if cur.rowcount == 0:
        raise KeyError(f"alert rule {rule_id} not found")


@dataclass(frozen=True)
class AlertEvent:
    event_id: int
    rule_id: int
    bottle_id: int
    bottle_name: str
    listing_id: int
    price_jpy: int
    reference_price_jpy: int | None
    message: str
    item_url: str | None


def evaluate(conn: sqlite3.Connection, *, now: datetime | None = None,
             max_age_hours: int = 72) -> list[AlertEvent]:
    """全ルールを評価し、通知すべきイベントを保存して返す (通知の送信はしない)。"""
    now = now or datetime.now(UTC)
    events: list[AlertEvent] = []
    rules = conn.execute(
        "SELECT r.*, b.display_name FROM price_alert_rules r JOIN bottles b ON b.id = r.bottle_id"
        " WHERE r.enabled = 1 ORDER BY r.id"
    ).fetchall()
    for rule in rules:
        best = current_best(conn, rule["bottle_id"], max_age_hours=max_age_hours, now=now)
        if best is None:
            continue  # 判断材料なし。armed は変えない
        if rule["kind"] == "target_price":
            ref = rule["target_price_jpy"]
            hit = best.price_jpy <= ref
            reason = f"目標価格 {ref:,} 円以下"
        else:
            series = history(conn, rule["bottle_id"], days=rule["lookback_days"] + 1, now=now)
            ref = reference_price(series, now.astimezone(JST).date(), rule["lookback_days"])
            if ref is None:
                continue
            hit = best.price_jpy <= ref * (1 - rule["drop_pct"] / 100)
            pct = (1 - best.price_jpy / ref) * 100
            reason = f"直近 {rule['lookback_days']} 日の相場 {ref:,} 円より {pct:.0f}% 安い"
        last = rule["last_fired_price_jpy"]
        fire = hit and (rule["armed"] or (last is not None and best.price_jpy < last))
        with conn:
            if not hit:
                conn.execute("UPDATE price_alert_rules SET armed = 1 WHERE id = ?", (rule["id"],))
                continue
            if not fire:
                continue
            shop = f"{best.shop_name} ({best.marketplace})" if best.shop_name else best.marketplace
            message = (f"値下がり: {rule['display_name']} が {best.price_jpy:,} 円 (税込・送料別)"
                       f" — {reason} / {shop}")
            ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            cur = conn.execute(
                "INSERT INTO price_alert_events (rule_id, bottle_id, listing_id, price_jpy,"
                " reference_price_jpy, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rule["id"], rule["bottle_id"], best.listing_id, best.price_jpy, ref, message, ts),
            )
            conn.execute(
                "UPDATE price_alert_rules SET armed = 0, last_fired_price_jpy = ?, last_fired_at = ?"
                " WHERE id = ?", (best.price_jpy, ts, rule["id"]),
            )
        assert cur.lastrowid is not None
        events.append(AlertEvent(cur.lastrowid, rule["id"], rule["bottle_id"], rule["display_name"],
                                 best.listing_id, best.price_jpy, ref, message, best.item_url))
    return events


# ===== 通知 =====

Sender = Callable[[AlertEvent], None]


def send_stdout(e: AlertEvent) -> None:
    print(e.message + (f"\n    {e.item_url}" if e.item_url else ""))


def send_macos(e: AlertEvent, run: Callable[..., object] = subprocess.run) -> None:
    """macOS の通知センターに表示する (osascript)。macOS 以外では何もしない。"""
    if platform.system() != "Darwin":
        return

    def q(s: str) -> str:  # AppleScript の文字列リテラル
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    script = f"display notification {q(e.message)} with title {q('Whisky AI 値下がり')}"
    run(["osascript", "-e", script], check=True, timeout=10)


def send_webhook(e: AlertEvent, url: str | None = None,
                 post: Callable[[str, bytes], None] | None = None) -> None:
    """Slack / Discord などの Incoming Webhook に JSON を POST する。

    ``text`` (Slack) と ``content`` (Discord) の両方を入れて、どちらでもそのまま表示されるようにする。
    """
    url = url or os.environ.get(WEBHOOK_ENV)
    if not url:
        raise RuntimeError(f"{WEBHOOK_ENV} is not set")
    text = e.message + (f"\n{e.item_url}" if e.item_url else "")
    body = json.dumps({"text": text, "content": text, "price_jpy": e.price_jpy,
                       "bottle_id": e.bottle_id}, ensure_ascii=False).encode("utf-8")
    (post or _post_json)(url, body)


def _post_json(url: str, body: bytes) -> None:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10):
        pass


SENDERS: dict[Channel, Sender] = {"stdout": send_stdout, "macos": send_macos, "webhook": send_webhook}


def notify(conn: sqlite3.Connection, events: Sequence[AlertEvent], channels: Sequence[Channel],
           senders: dict[Channel, Sender] | None = None) -> list[str]:
    """イベントを各通知先に送り、送れた通知先を記録する。失敗はエラー文字列で返す (処理は続ける)。"""
    senders = senders or SENDERS
    errors = []
    for e in events:
        sent = []
        for ch in channels:
            try:
                senders[ch](e)
                sent.append(ch)
            except Exception as ex:  # 1 つの通知先の失敗で他を止めない
                errors.append(f"{ch}: {type(ex).__name__}: {ex}")
        with conn:
            conn.execute("UPDATE price_alert_events SET notified_channels = ? WHERE id = ?",
                         (",".join(sent) or None, e.event_id))
    return errors


def default_channels() -> list[Channel]:
    channels: list[Channel] = ["stdout"]
    if platform.system() == "Darwin":
        channels.append("macos")
    if os.environ.get(WEBHOOK_ENV):
        channels.append("webhook")
    return channels
