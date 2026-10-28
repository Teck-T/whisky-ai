# Whisky AI データ基盤 設計書

対象: PR 1（データベース設計と商品同一性判定）と PR 2（価格収集モジュール、§7）。

## 1. 目的と範囲

ローカル LLM（Apple M3 Max 48GB 上の Qwen 系モデル）がレビューから抽出した香味特徴 JSON と、
商品・価格データを永続化するバックエンドを作る。LLM の学習と推論はこのリポジトリの範囲外。

| 要件 | 対応箇所 |
|---|---|
| 1. 蒸留所 / 銘柄 / リリース / バッチ / 容量・度数 / 販売商品 を区別する | §3, `0001_initial.sql` |
| 2. JAN・商品名・容量・度数・バッチ番号から同一商品を照合する | §4, `matching.py`, `normalize.py` |
| 3. 不確実な照合は自動統合せず「要確認」として保存する | §4.4, `match_candidates` |
| 4. 楽天 / Yahoo! API の仕様・規約の調査 | [`api_research.md`](api_research.md) |
| 5. 価格収集モジュール（APIキーなしでもモックで試験可能） | §7, `collectors/` |
| 6. 価格履歴・送料・税込・在庫・取得日時・販売店・URL・情報源を保存する | `price_observations`, `shops`, `listings` |
| 7. メーカー希望小売価格と実勢価格を混同しない。出典のない定価は作らない | §5 |
| 8. LLM の香味特徴 JSON を格納できるスキーマ | §6, `flavor.py` |

## 2. 全体構成

```
src/whisky_ai/
  migrations/0001_initial.sql   スキーマ (番号付き SQL、schema_migrations で版管理)
  db.py                         接続 (外部キー ON) / マイグレーション適用
  normalize.py                  商品名・容量・度数・年数・バッチ・JAN の正規化
  matching.py                   同一性判定 (Matcher) / 結果の保存 / レビュー操作
  repository.py                 書き込み API (SQL はここに集約)
  flavor.py                     香味 JSON の検証・保存
  sample_data.py                架空サンプルデータ (62 銘柄 / 143 ボトル)
  cli.py                        whisky-ai コマンド
```

実行時の依存は標準ライブラリだけ（`sqlite3`）。ORM は使っていない。
SQL を `repository.py` / `matching.py` / `flavor.py` にまとめ、PostgreSQL へ移すときに書き換える範囲を限定している（§8）。

## 3. データモデル

ER 図: [`er_diagram.md`](er_diagram.md)

### 3.1 階層の考え方

| 層 | テーブル | 例（架空） | 判断基準 |
|---|---|---|---|
| 蒸留所 | `distilleries` | Aokawa Distillery | 物理的な生産拠点 |
| 銘柄 | `brands` | Aokawa / Aokawa Blended | 市場に出るブランド。ブレンデッドは複数の蒸留所を持つため `brand_distilleries` で多対多 |
| リリース | `releases` | 12 Years / Sherry Cask 12 Years | 中身の仕様（熟成年数・樽・エディション） |
| バッチ | `batches` | Batch No.2 | 同じリリースのロット違い。`has_batches=1` のリリースだけが持つ |
| ボトル | `bottles` | 12 Years **700ml 43%** | 物理 SKU = リリース × バッチ × 容量 × 度数 × 包装。**照合の到達点** |
| 識別子 | `product_identifiers` | JAN 2900000000123 | ボトルに付く JAN/EAN/UPC |
| 販売商品 | `listings` | 楽天のある店舗の出品 | マーケットプレイスの 1 出品。照合が確定するまで `bottle_id` は NULL |

* **度数はボトル側**に置く。輸出向け 40% と国内向け 43% のように、同じリリースで度数だけ違う商品があるため。
* `bottles` の一意制約は `(release_id, COALESCE(batch_id,0), volume_ml, abv_bp, packaging)`。
  `batch_id` が NULL の行も重複を防げるよう、式インデックスにしている。
* JAN は **ボトルごとに一意とは限らない**。メーカーがバッチ違いで JAN を使い回すことがあるため、
  一意制約は `(id_type, value, bottle_id)` にしている。照合ロジックはこの使い回しを前提に作っている（§4.2）。
* `aliases` は日本語名・英語名・略称の表記ゆれ辞書。照合時に「銘柄の別名 × リリースの別名」の組み合わせで名前候補を作る。

### 3.2 出典 (`sources`)

マスターの全行、価格観測、定価が `source_id NOT NULL` で出典を持つ。
`kind` には `official_site` / `press_release` / `api` / `retailer_page` / `manual_entry` / `sample` がある。
サンプルデータは `sample` で、実在の情報と見分けられる。

### 3.3 型の規約

| 概念 | SQLite | PostgreSQL 移行時 |
|---|---|---|
| 主キー | `INTEGER PRIMARY KEY` | `BIGINT GENERATED ALWAYS AS IDENTITY` |
| 日時 | `TEXT` UTC ISO8601 `YYYY-MM-DDTHH:MM:SSZ` | `TIMESTAMPTZ` |
| 真偽 | `INTEGER` + `CHECK IN (0,1)` | `BOOLEAN` |
| JSON | `TEXT` + `CHECK (json_valid(...))` | `JSONB` |
| 金額 | `INTEGER`（円） | `INTEGER` |
| 度数 | `INTEGER`（1/100 %、43.0% = 4300） | `INTEGER` |

浮動小数を使わないことで、度数の等号比較を安全にしている（43.0 と 43.00000001 のような誤差が出ない）。

## 4. 商品同一性判定

### 4.1 正規化（`normalize.py`）

1. NFKC 正規化（全角英数 → 半角）、小文字化、長音記号の統一。
2. 販促語の除去: 【送料無料】、正規品、箱付き、ギフト、ポイント◯倍 など。
   括弧は、中身が販促語だけなら捨てる。`(Sherry Cask)` のような商品情報なら残す。
3. 仕様の抽出:
   * 容量: `700ml`、`70cl`、`0.7L`、`1.75l` → ml の整数
   * 度数: `43%`、`43度`、`ABV 46.3` → 1/100 % 単位
   * 年数: `12年`、`12 Years Old`、`18yo`（`2024年` のような西暦は年数として扱わない）
   * バッチ: `Batch No.03`、`バッチ2`、`ロット #A12` → `3`、`2`、`A12`
4. 名前トークン: 上の仕様表記と「ウイスキー」などの一般語を除いた残り。
5. JAN: GTIN-8/12/13/14 のチェックディジットを検証する。UPC-A は先頭に 0 を付けて EAN-13 にそろえる。
   **チェックディジットが合わない JAN は照合に使わない**（理由 `jan_invalid_ignored` を記録する）。

### 4.2 判定アルゴリズム（`matching.Matcher`）

**ハード制約**: 出品とボトルの両方で値がわかっていて、容量・度数・年数・バッチのどれかが食い違うボトルは候補から外す。

**(A) JAN がカタログにある場合**

| 状況 | 判定 |
|---|---|
| JAN に該当するボトルが仕様の制約で 1 つに絞れ、名前もある程度一致 | **自動照合** (`jan`) |
| JAN に複数のボトルが該当し（使い回し）、バッチや度数で絞れない | 要確認 `jan_shared_by_multiple_bottles` |
| JAN は一致するが仕様が矛盾（例: JAN は 700ml、タイトルは 180ml） | 要確認 `jan_match_but_spec_conflict` |
| JAN は一致するが名前がほとんど一致しない（出品者の誤入力など） | 要確認 `jan_match_but_name_mismatch` |

**(B) JAN がない、またはカタログにない場合（名前 + 仕様）**

* 名前スコア = 再現率 ×（0.90 + 0.10 × 被覆率）
  * 再現率: カタログ名のトークンのうち出品名に含まれる割合。
    日本語は空白で区切られないことが多いので、2 文字以上のトークンは部分文字列一致で判定する。
  * 被覆率: 出品名のうちカタログ名で説明できる割合。同点のときの順位付けに使う。
* **他の候補にしかない語による減点 (−0.15)**: 「Hoshizora Sherry Cask 12」は「Hoshizora 12」にも再現率 1.0 で一致してしまう。
  そこで、出品名に別の候補だけが持つ語（`sherry`、`blended` など）が含まれていれば、その語を持たない候補を減点する。
* 自動照合には、次の **すべて** を満たす必要がある:
  1. スコアが 0.90 以上
  2. 次点との差が 0.05 以上
  3. 出品の容量がわかっている
  4. ボトルに熟成年数があるなら、出品にも年数の表記がある
  5. リリースがバッチ制なら、出品にもバッチ番号がある
  6. 出品の JAN が、そのボトルに登録済みの別の JAN と食い違っていない
* どれかを満たさない場合は **要確認** とし、スコア 0.50 以上の候補を最大 5 件保存する。理由は JSON で記録する。

閾値は `MatchConfig` で変更できる。

### 4.3 判定例（架空カタログ。テストで検証済み）

| 出品タイトル | 判定 | 理由 |
|---|---|---|
| `【送料無料】Aokawa 12 Years 700ml 正規品` | 自動 → Aokawa 12 Years 700ml | |
| `星空 シェリー樽 12年 700ml` | 自動 → Hoshizora Sherry Cask 12 Years | 日本語の別名 |
| `Mistral Peak Original 700ml` | 要確認（候補 2 件） | 40% と 43% を区別できない |
| `Mistral Peak Original 700ml 43%` | 自動 → 43% 版 | |
| `黒鉄 カスクストレングス 700ml` + 共有 JAN | 要確認 | JAN をバッチ 1 と 2 で共有 |
| 上記 + `バッチ2` または `57.6%` | 自動 → Batch 2 | バッチ番号か度数で 1 つに絞れる |
| `Aokawa Original`（容量なし） | 要確認 | `volume_unknown` |
| `Shirakaba 18年 700ml` | 照合しない | カタログに 18 年がない。12 年に寄せない |
| 登録とは別の JAN + `Aokawa 12年 700ml` | 要確認 | `jan_differs_from_catalog`（リニューアルの可能性） |

### 4.4 要確認ワークフロー

```
listings.match_status:
  unprocessed ──match──▶ matched          (bottle_id 確定)
              ├────────▶ pending_review   (match_candidates に候補を保存。bottle_id は NULL)
              └────────▶ unmatched
  pending_review ──accept_candidate──▶ matched (match_method='manual')
                 └─reject_listing────▶ rejected
```

* テーブルの CHECK 制約で `match_status='matched'` と `bottle_id IS NOT NULL` を同値にしている。
  そのため、未確定の出品が価格集計に混ざらない（`latest_prices_for_bottle` は matched だけを返す）。
* 人が確定した結果（`manual`）は、自動の再判定で上書きしない。
* 出品のタイトルや JAN が変わったら `unprocessed` に戻し、判定をやり直す。
* `accept --register-jan` を使うと、出品の JAN を承認したボトルに登録できる（出典は `manual_entry`）。
  以後はその JAN で自動照合される。

## 5. 価格の区別（実勢価格 / 希望小売価格）

| | 実勢価格 | メーカー希望小売価格 |
|---|---|---|
| テーブル | `price_observations`（追記のみ） | `retail_price_references` |
| 紐付け先 | `listings`（出品） | `bottles` |
| 出典 | API などの `source_id` | **必須**。公式サイトやプレスリリースなど、メーカーが公表したもの |
| 禁止事項 | — | `add_retail_price_reference` は `source_id=None` を拒否する。`kind in ('api','retailer_page')` の出典も拒否する |

* **税込価格**: 表示価格が税込とわかる場合だけ自動で埋める。税抜表示の場合は API が税込額を返したときだけ保存し、
  こちらで推計はしない（店舗ごとに端数処理が違うため）。不明な場合は NULL。
* **送料**: 送料額（`shipping_fee_jpy`）と条件（`shipping_condition`）を分けて持つ。
  Yahoo! の API は送料「条件」しか返さないため、送料額を NULL のまま保存できるようにしている（[`api_research.md`](api_research.md)）。
* オープン価格は、メーカーが「オープン価格」と公表した事実を `price_type='open_price_announced'` として記録できる。
  金額はでっち上げない。現状のスキーマでは `price_jpy > 0` が必要なので、金額のない記録の扱いは今後の課題とする（§9）。

## 6. 香味特徴 JSON（LLM 出力）

* `flavor_extractions.payload` に LLM の出力原文を **必ず** 保存する（壊れた JSON も `{"_raw": ...}` で保存）。
  不正な出力もプロンプト改善の材料になるため。
* 再現性のため、`model_name` / `model_revision` / `prompt_version` / `schema_version` も記録する。
* 検証を通った出力だけを `flavor_notes`（nose/palate/finish/overall の記述子）と
  `flavor_axes`（sweetness、smokiness などの 0..1 の数値軸）に展開する。推薦や検索はこの展開済みテーブルを使う。
* 紐付け先は review / release / batch / bottle のいずれか。バッチ単位の香味差も表現できる。
* スキーマ v1 の例は `flavor.py` の docstring にある。軸の追加は `schema_version` を上げて行う。

## 7. 価格収集（PR 2）の構成

```
src/whisky_ai/collectors/
  base.py        共通型: SearchQuery / Offer / SearchResult / Collector プロトコル
  http.py        urllib による最小 HTTP 層 (Transport を差し替えてテスト)
  rate_limit.py  最小間隔 1 秒のレートリミッタ、429/5xx の指数バックオフ (Retry-After 優先)
  rakuten.py     楽天市場商品検索 API 2026-07-01 (applicationId + accessKey、任意で Referer)
  yahoo.py       Yahoo!ショッピング商品検索 API v3 (appid、jan_code 検索)
  mock.py        フィクスチャ (fixtures/*.json、架空データ) を返す Transport
  factory.py     環境変数から Collector を作る。キーがなければ mock
  pipeline.py    検索 → 生レスポンス保存 → upsert_listing → 価格観測を追記 → resolve_listing
```

* **Collector は DB に触れない。** API の 1 商品を `Offer` に変換するだけで、不明な値は None のまま渡す。
  保存と照合は `pipeline.collect` が PR 1 の `repository` / `matching` を使って行う。
* **API キー**: 環境変数（`.env.example`）から読む。楽天は 2 つのキーが揃っていないと実通信しない。
  キーがないときは `marketplace='mock'`、出典 `kind='sample'` のフィクスチャを使い、実データと混ざらないようにする。
  テストはすべて FakeTransport かフィクスチャで動き、実通信しない。
* **キーの漏洩防止**: 生レスポンスの `request_params` には認証パラメータを入れない。
  `repository.add_raw_response` は `applicationId`、`accessKey`、`affiliateId`、`appid` を含む dict を拒否する。
  エラーメッセージにもキーを含めない。
* **レート制限**: 1 つの Collector インスタンスにつき最小間隔 1 秒。429/5xx は 2→4→8 秒の指数バックオフで最大 3 回まで再試行し、
  `Retry-After` があればそれを優先する。`whisky-ai watch run` は DB ごとのロックファイルで多重起動を防ぐ（§7.1）。それ以外の経路で複数プロセスを同時に動かす場合の制御はしていない。
* **フィールドの対応**（[`api_research.md`](api_research.md)）:

| 保存先 | 楽天 | Yahoo! |
|---|---|---|
| `price_jpy` | `itemPrice` | `price` |
| `price_tax_included` | `taxFlag` 0→税込、1→税抜 | `taxExcludePrice` が併記され、`price` より小さいときだけ税込。それ以外は NULL |
| `shipping_condition` | `postageFlag` 0→`included`、1→`separate` | `shipping.code` 2→`free`、3→`conditional_free`、1→`unknown` |
| `shipping_fee_jpy` | NULL（API に金額がない） | NULL（同左）。`free`/`included` のときだけ 0 |
| `in_stock` | `availability` | `inStock` |
| `jan_raw` | なし（API にない） | `janCode`（空なら NULL） |
| `external_item_id` / 販売店 | `itemCode` / `shopCode`、`shopName`、`shopUrl` | `code` / `seller.sellerId`、`name`、`url` |

* **生レスポンスの保持**: `WHISKY_AI_RAW_RETENTION_DAYS`（既定 30 日。0 なら保存しない）で `expires_at` を決める。
  `whisky-ai purge-raw` で期限切れを削除する。価格観測は残し、生レスポンスへの参照だけ外す。
* **手動確定の保護**: 同じ出品を再収集すると価格観測は追記されるが、人が確定した照合（`manual`）は上書きしない。
  タイトルや JAN が変わった場合だけ再照合する（§4.4）。
* **実エンドポイントの確認**: 2026-10-27 にキーなしで各 1 回リクエストし、楽天は HTTP 400
  `applicationId must be present ...`、Yahoo! は HTTP 401 `Authentication parameters ... incompleted` が返ることを確認した。
  URL が実在することと、エラーの形式はこれで確かめた。キーを使った正常系の通信は未検証（キーがないため）。

### 7.1 監視リストと定期収集（PR 3）

* `watch_targets`（マイグレーション 0002）に「どのマーケットプレイスで、何を検索するか」を登録する。
  * `watch add --bottle-id N` は、カタログのボトルから検索条件を作る。楽天は日本語名（銘柄 + リリース）のキーワード、
    Yahoo! はボトルに JAN があれば JAN で検索する（名前より正確なため）。
  * 楽天は JAN 検索ができないので、楽天の対象はキーワードが必須（アプリと DB の CHECK の両方で防ぐ）。
* `watch run` は、前回実行から `min_interval_hours`（既定 24 時間）が過ぎた対象だけを収集する。
  launchd から 1 日 2 回起動しても、API へのリクエストは対象ごとに 1 日 1 回に抑えられる。
  * マーケットプレイスごとに Collector を 1 つだけ作り、1 回の実行の中でレート制限を共有する。
  * 1 件が失敗しても残りは続け、対象ごとに `last_status` / `last_error` を記録する（`watch list` で確認できる）。
  * DB と同じ場所のロックファイル（`*.lock`）で多重起動を防ぐ。
* macOS では `scripts/run_watch.sh`（`.env` を読み込み、ログを `data/logs/` に追記）を
  `deploy/macos/com.whisky-ai.watch.plist` から launchd で起動する。

### 7.2 接続チェック（PR 3）

`whisky-ai check-api` は、実キーで初めて試すときに失敗の原因を切り分けるためのコマンド。

* 環境変数が揃っているかを表示する。キーは先頭 4 文字だけ見せる。
* このマシンのグローバル IPv4 を表示する（楽天の「許可された IP アドレス」と見比べるため。`api.ipify.org` に問い合わせる）。
* 検索を 1 回だけ行い、DB には保存しない。失敗したら、HTTP ステータスから考えられる原因を示す
  （楽天の 403 → IP 制限か Referer、400/401 → キーの値）。

## 8. PostgreSQL への移行

1. §3.3 の型対応で `0001_initial.sql` を PG 用の DDL に書き換える。
   SQLite 固有の構文（AUTOINCREMENT、WITHOUT ROWID、型なしの列）は使っていない。
   `json_valid` の CHECK は JSONB 型に置き換える。
2. プレースホルダ `?` を `%s` に変える（psycopg の場合）。対象は repository / matching / flavor の 3 ファイルだけ。
3. `ON CONFLICT ... DO UPDATE` と `COALESCE` の式インデックスは、PG でもそのまま使える。
4. `INSERT OR IGNORE` の 2 か所は `ON CONFLICT DO NOTHING` に書き換える。

## 9. 今後の課題

* **実在商品のマスターデータ**: 今回は投入していない。投入するときは、メーカーの公式サイトやプレスリリースを
  `sources` に登録したうえで行い、未確認の値は入れない。
* **照合の性能**: 現在はカタログ全件を総当たりで走査している（143 ボトルで 1 件あたり約 0.7 ms）。
  数万 SKU になったら、トークンの転置インデックスか、SQLite FTS5 / PG `pg_trgm` で候補を先に絞り込む。
* **表記ゆれ辞書の拡充**: カタカナ表記の違い（グレン / グレン・）、略称、旧ラベル名など。レビューで承認した結果から別名を学習する仕組み。
* **オープン価格の扱い**: 金額のない「オープン価格」記録を許可するスキーマ変更（`price_jpy` を NULL 可にする）。
* **セット商品・グラス付き・複数本セット**の検出（現状は単品として照合を試み、容量から外れることが多い）。
* **レビュー UI**: 現状は CLI だけ。
* **実キーでの疎通確認**: API キーを取得したら `whisky-ai check-api` で接続を確かめ、正常系のレスポンスでフィールドの対応を確認する（特に楽天 2026-07-01 版の formatVersion=2 と、Yahoo! の税込判定）。
* **監視キーワードの調整**: 自動生成のキーワードは日本語名（銘柄 + リリース）。英語表記の出品を取りこぼす場合は、英語キーワードの監視対象を追加する。
* **楽天の Referer**: 新 API で必須かどうかが二次情報しかない。必要なら `RAKUTEN_REFERER` を設定する。
* **価格データの保持期限**: 各 API の規約の確認が終わり次第、`api_raw_responses.expires_at` の既定値を決める（[`api_research.md`](api_research.md) の未確認事項）。
