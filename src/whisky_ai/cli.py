"""コマンドラインインターフェース。

    whisky-ai init-db              マイグレーション適用
    whisky-ai seed-sample          架空サンプルカタログ + モック出品を投入
    whisky-ai match                未処理の出品を照合
    whisky-ai review list          要確認キューを表示
    whisky-ai review accept ID     候補を承認
    whisky-ai review reject LISTING_ID
    whisky-ai stats                件数の概要
    whisky-ai collect rakuten|yahoo --keyword KW [--jan JAN] [--page N] [--mock]
    whisky-ai prices BOTTLE_ID     照合確定済み出品の最新価格
    whisky-ai purge-raw            保持期限切れの API 生レスポンスを削除
    whisky-ai check-api [rakuten|yahoo]  実 API への接続チェック (1 回だけ検索、保存しない)
    whisky-ai watch add --bottle-id N | --keyword KW [--source rakuten|yahoo|both]
    whisky-ai watch list | enable ID | disable ID
    whisky-ai watch run [--mock] [--all]  期限の来た監視対象を収集 (cron / launchd 向け)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from whisky_ai import db, diagnostics, repository, sample_data, watchlist
from whisky_ai.collectors.base import SearchQuery
from whisky_ai.collectors.factory import build_collector
from whisky_ai.collectors.pipeline import collect
from whisky_ai.collectors.rakuten import CollectorError
from whisky_ai.collectors.rate_limit import RateLimitExceeded
from whisky_ai.matching import Catalog, Matcher, accept_candidate, reject_listing, resolve_unprocessed


def _cmd_init(conn: sqlite3.Connection, _: argparse.Namespace) -> int:
    applied = db.migrate(conn)
    print(f"applied: {', '.join(applied) if applied else '(up to date)'}")
    return 0


def _cmd_seed(conn: sqlite3.Connection, _: argparse.Namespace) -> int:
    db.migrate(conn)
    if conn.execute("SELECT COUNT(*) FROM bottles").fetchone()[0]:
        print("catalog is not empty; seed-sample expects a fresh database", file=sys.stderr)
        return 1
    cat = sample_data.load_sample_catalog(conn)
    with conn:
        shop = repository.upsert_shop(conn, "mock", "mock-shop", "モック酒店 (架空)")
        src = repository.add_source(conn, "sample", "モック出品 (架空)")
        for item in sample_data.sample_listings(cat):
            lid = repository.upsert_listing(conn, repository.ListingData(
                "mock", item.external_item_id, item.title, jan_raw=item.jan, shop_id=shop,
                item_url=f"https://example.invalid/items/{item.external_item_id}",
            ))
            repository.record_price_observation(conn, repository.PriceObservation(
                lid, item.price_jpy, src, price_tax_included=True, shipping_condition="unknown",
                in_stock=True,
            ))
    print(f"brands={len(cat.brand_ids)} bottles={len(cat.bottle_ids)}")
    return 0


def _cmd_match(conn: sqlite3.Connection, _: argparse.Namespace) -> int:
    counts = resolve_unprocessed(conn, Matcher(Catalog.load(conn)))
    print(json.dumps(counts, ensure_ascii=False))
    return 0


def _cmd_review(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.action == "list":
        rows = conn.execute(
            """
            SELECT mc.id, mc.listing_id, l.title, b.display_name, mc.score, mc.reasons
            FROM match_candidates mc
            JOIN listings l ON l.id = mc.listing_id
            JOIN bottles b ON b.id = mc.bottle_id
            WHERE mc.status = 'pending'
            ORDER BY mc.listing_id, mc.score DESC
            """
        ).fetchall()
        for r in rows:
            reasons = json.loads(r["reasons"])
            print(f"[{r['id']}] listing={r['listing_id']} score={r['score']:.3f}\n"
                  f"    出品: {r['title']}\n    候補: {r['display_name']}\n"
                  f"    理由: {', '.join(reasons['listing'] + reasons['candidate'])}")
        print(f"{len(rows)} pending candidate(s)")
    elif args.action == "accept":
        accept_candidate(conn, args.id, args.by, register_jan=args.register_jan)
        print(f"accepted candidate {args.id}")
    elif args.action == "reject":
        reject_listing(conn, args.id, args.by)
        print(f"rejected listing {args.id}")
    return 0


def _cmd_stats(conn: sqlite3.Connection, _: argparse.Namespace) -> int:
    for table in ("distilleries", "brands", "releases", "batches", "bottles", "product_identifiers",
                  "listings", "match_candidates", "price_observations", "flavor_extractions"):
        print(f"{table:22s} {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
    for r in conn.execute("SELECT match_status, COUNT(*) FROM listings GROUP BY 1 ORDER BY 1"):
        print(f"  listings.{r[0]:16s} {r[1]}")
    return 0


def _cmd_collect(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    collector = build_collector(args.source, force_mock=args.mock)
    if collector.is_mock:
        print(f"[mock] {args.source} の API キーが未設定 (または --mock) のためフィクスチャを使用します",
              file=sys.stderr)
    try:
        query = SearchQuery(keyword=args.keyword, jan=args.jan, page=args.page)
        summary = collect(conn, collector, query)
    except (ValueError, CollectorError, RateLimitExceeded) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"offers": summary.offers, "observations": summary.observations,
                      "match_status": dict(summary.match_status)}, ensure_ascii=False))
    for w in summary.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def _cmd_prices(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    rows = repository.latest_prices_for_bottle(conn, args.bottle_id)
    for r in rows:
        incl = "?" if r["tax_included_price_jpy"] is None else f"{r['tax_included_price_jpy']:,}"
        ship = "?" if r["shipping_fee_jpy"] is None else f"{r['shipping_fee_jpy']:,}"
        stock = {1: "在庫あり", 0: "在庫なし"}.get(r["in_stock"], "在庫不明")
        print(f"{r['observed_at']} {r['marketplace']:8s} 税込 {incl} 円 / 送料 {ship} 円"
              f" ({r['shipping_condition']}) {stock} {r['shop_name'] or ''}\n    {r['item_url'] or ''}")
    print(f"{len(rows)} listing(s)")
    return 0


def _cmd_purge_raw(conn: sqlite3.Connection, _: argparse.Namespace) -> int:
    print(f"purged {repository.purge_expired_raw_responses(conn)} raw response(s)")
    return 0


def _cmd_check_api(_: sqlite3.Connection, args: argparse.Namespace) -> int:
    sources: tuple[watchlist.Source, ...] = (
        ("rakuten", "yahoo") if args.source == "all" else (args.source,))
    ok = True
    for source in sources:
        res = diagnostics.check_api(source)
        print("\n".join(res.lines))
        ok &= res.ok
    return 0 if ok else 1


def _cmd_watch(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.action == "add":
        sources: tuple[watchlist.Source, ...] = (
            ("rakuten", "yahoo") if args.source == "both" else (args.source,))
        if args.bottle_id is not None:
            ids = watchlist.add_targets_for_bottle(conn, args.bottle_id, sources, keyword=args.keyword,
                                                   min_interval_hours=args.interval_hours)
        elif args.keyword or args.jan:
            ids = [watchlist.add_target(conn, s, label=args.label or args.keyword or args.jan,
                                        keyword=args.keyword, jan=args.jan if s == "yahoo" else None,
                                        min_interval_hours=args.interval_hours) for s in sources]
        else:
            print("error: --bottle-id か --keyword / --jan を指定してください", file=sys.stderr)
            return 2
        print(f"added watch target(s): {ids}")
    elif args.action == "list":
        for r in conn.execute("SELECT * FROM watch_targets ORDER BY id"):
            query = f"jan={r['jan']}" if r["jan"] else f"keyword={r['keyword']!r}"
            state = "on " if r["enabled"] else "off"
            last = f"{r['last_run_at']} {r['last_status']}" if r["last_run_at"] else "未実行"
            print(f"[{r['id']}] {state} {r['marketplace']:7s} {query} every {r['min_interval_hours']}h"
                  f" | {r['label']} | {last}" + (f" ({r['last_error']})" if r["last_error"] else ""))
    elif args.action in ("enable", "disable"):
        watchlist.set_enabled(conn, args.id, args.action == "enable")
        print(f"{args.action}d watch target {args.id}")
    elif args.action == "run":
        lock = db.resolve_db_path(args.db).with_suffix(".lock")
        with watchlist.single_instance(Path(lock)) as acquired:
            if not acquired:
                print("another watch run is in progress; skipped", file=sys.stderr)
                return 0
            report = watchlist.run_due(conn, force_mock=args.mock, run_all=args.all)
        print(json.dumps({"ran": report.ran, "match_status": report.match_status,
                          "errors": len(report.errors)}, ensure_ascii=False))
        for e in report.errors:
            print(f"error: {e}", file=sys.stderr)
        return 1 if report.errors else 0
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="whisky-ai")
    p.add_argument("--db", help=f"SQLite path (default: ${db.DEFAULT_DB_ENV} or {db.DEFAULT_DB_PATH})")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db").set_defaults(func=_cmd_init)
    sub.add_parser("seed-sample").set_defaults(func=_cmd_seed)
    sub.add_parser("match").set_defaults(func=_cmd_match)
    sub.add_parser("stats").set_defaults(func=_cmd_stats)
    col = sub.add_parser("collect", help="マーケットプレイス API から価格を収集")
    col.add_argument("source", choices=["rakuten", "yahoo"])
    col.add_argument("--keyword")
    col.add_argument("--jan", help="JAN で検索 (Yahoo! のみ)")
    col.add_argument("--page", type=int, default=1)
    col.add_argument("--mock", action="store_true", help="API キーがあってもフィクスチャを使う")
    col.set_defaults(func=_cmd_collect)
    pr = sub.add_parser("prices")
    pr.add_argument("bottle_id", type=int)
    pr.set_defaults(func=_cmd_prices)
    sub.add_parser("purge-raw").set_defaults(func=_cmd_purge_raw)
    chk = sub.add_parser("check-api", help="実 API への接続チェック")
    chk.add_argument("source", nargs="?", choices=["rakuten", "yahoo", "all"], default="all")
    chk.set_defaults(func=_cmd_check_api)
    watch = sub.add_parser("watch", help="監視対象の管理と定期収集")
    watch.set_defaults(func=_cmd_watch)
    wsub = watch.add_subparsers(dest="action", required=True)
    wadd = wsub.add_parser("add")
    wadd.add_argument("--bottle-id", type=int)
    wadd.add_argument("--keyword")
    wadd.add_argument("--jan", help="Yahoo! のみ JAN で検索")
    wadd.add_argument("--label")
    wadd.add_argument("--source", choices=["rakuten", "yahoo", "both"], default="both")
    wadd.add_argument("--interval-hours", type=int, default=24)
    wsub.add_parser("list")
    for name in ("enable", "disable"):
        wsub.add_parser(name).add_argument("id", type=int)
    wrun = wsub.add_parser("run")
    wrun.add_argument("--mock", action="store_true", help="API キーがあってもフィクスチャを使う")
    wrun.add_argument("--all", action="store_true", help="間隔を無視して全対象を収集")
    review = sub.add_parser("review")
    review.set_defaults(func=_cmd_review)
    rsub = review.add_subparsers(dest="action", required=True)
    rsub.add_parser("list")
    acc = rsub.add_parser("accept")
    acc.add_argument("id", type=int, help="match_candidates.id")
    acc.add_argument("--by", default="cli")
    acc.add_argument("--register-jan", action="store_true",
                     help="出品の JAN を承認したボトルの識別子として登録する")
    rej = rsub.add_parser("reject")
    rej.add_argument("id", type=int, help="listings.id")
    rej.add_argument("--by", default="cli")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = db.connect(args.db)
    try:
        if args.command not in ("init-db", "seed-sample"):
            db.migrate(conn)
        return int(args.func(conn, args))
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
