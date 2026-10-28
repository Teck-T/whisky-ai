# whisky-ai

ウイスキー推薦とオンライン価格監視のためのデータ基盤（Python 3.13 + SQLite）。

ローカル LLM（Qwen 系）がレビューから抽出した香味特徴 JSON と、商品・価格データを永続化します。
LLM の学習と推論はこのリポジトリの範囲外です。

## ドキュメント

* [設計書](docs/design.md): データモデル、商品同一性判定、価格の区別、今後の課題
* [ER図](docs/er_diagram.md)（Mermaid）
* [楽天 / Yahoo! API 調査メモ](docs/api_research.md): 最新仕様、利用条件、データ保存条件

## 機能

* 蒸留所 → 銘柄 → リリース → バッチ → ボトル（容量・度数）→ 販売商品 を区別した正規化スキーマ
* JAN/EAN（チェックディジット検証）、商品名、容量、度数、熟成年数、バッチ番号による同一商品の照合
* 不確実な照合は自動統合せず、要確認キュー（`match_candidates`）に保存し、CLI で承認または却下
* 価格履歴（税込 / 税抜、送料、在庫、取得日時、販売店、URL、情報源）のスキーマと書き込み API
* メーカー希望小売価格は別テーブルに保存し、出典がないと登録できない
* LLM 香味特徴 JSON の検証と保存（不正な出力も原文を保存する）

* 楽天市場 / Yahoo!ショッピングの商品検索 API による価格収集と、監視リストによる定期収集。API キーがなければ架空のフィクスチャで動きます（[設計書 §7](docs/design.md#7-価格収集pr-2の構成)）

## セットアップ（macOS / Linux）

依存パッケージは開発ツールだけです（実行時は標準ライブラリのみ）。

```bash
# uv を使う場合 (推奨)
uv venv -p 3.13
uv pip install -e ".[dev]"
source .venv/bin/activate

# または標準の venv
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

macOS で Python 3.13 がない場合は `brew install python@3.13` か `uv python install 3.13` で入れてください。

設定は環境変数で行います（`.env.example` を参照）。

| 変数 | 用途 |
|---|---|
| `WHISKY_AI_DB` | SQLite ファイルのパス（既定: `data/whisky_ai.sqlite3`） |
| `RAKUTEN_APPLICATION_ID` / `RAKUTEN_ACCESS_KEY` | 楽天 API（両方必須） |
| `RAKUTEN_REFERER` / `RAKUTEN_AFFILIATE_ID` | 楽天 API（任意。Referer はアプリ登録時のサイト URL） |
| `YAHOO_CLIENT_ID` | Yahoo!ショッピング API |
| `WHISKY_AI_RAW_RETENTION_DAYS` | API 生レスポンスの保持日数（既定 30。0 で保存しない） |

API キーはリポジトリにコミットしないでください（`.env` は `.gitignore` 済み）。

## 使い方（サンプルデータでの動作確認）

```bash
whisky-ai init-db          # マイグレーションを適用
whisky-ai seed-sample      # 架空カタログ (62銘柄/143ボトル) とモック出品を投入
whisky-ai match            # 未処理の出品を照合
whisky-ai stats
whisky-ai review list      # 要確認キュー
whisky-ai review accept <candidate_id> [--register-jan]
whisky-ai review reject <listing_id>

# 価格収集 (API キー未設定ならフィクスチャを使用)
whisky-ai collect rakuten --keyword "Aokawa"
whisky-ai collect yahoo --jan 2900000000049
whisky-ai prices <bottle_id>  # 照合確定済み出品の最新価格
whisky-ai purge-raw           # 保持期限切れの API 生レスポンスを削除
```

API キーを設定すると実 API に接続します。1 リクエスト/秒以下に抑え、429 を受けたら待って再試行します。
キーがない場合、収集データは `marketplace='mock'` として保存され、実データとは区別されます。

別の DB を使う場合は `whisky-ai --db path/to.sqlite3 ...` と指定します。

Python からの利用例:

```python
from whisky_ai import db
from whisky_ai.matching import Catalog, Matcher, ListingInput

conn = db.connect()
db.migrate(conn)
matcher = Matcher(Catalog.load(conn))
r = matcher.match(ListingInput("【送料無料】Aokawa 12年 700ml 正規品"))
print(r.decision, r.bottle_id, r.reasons)
```

## 実 API への接続チェック

API キーを `.env` に書いたら、最初にこれを実行してください。

```bash
set -a; source .env; set +a
whisky-ai check-api          # 楽天と Yahoo! の両方。片方だけなら check-api rakuten
```

設定されている環境変数（キーは一部だけ表示）、このマシンのグローバル IPv4、テスト検索の結果を表示します。
楽天で 403 が出たら、表示された IPv4 が楽天アプリ設定の「許可された IP アドレス」と一致しているか、
`RAKUTEN_REFERER` がアプリ URL と一致しているかを確認してください。

## 監視リストと定期収集

```bash
whisky-ai watch add --bottle-id 12          # カタログのボトルから楽天/Yahoo! の監視対象を作る
whisky-ai watch add --keyword "山崎 12年" --source rakuten
whisky-ai watch list                         # 前回の実行結果とエラーも表示
whisky-ai watch run                          # 前回から 24 時間以上たった対象だけ収集
whisky-ai watch run --mock                   # キーがなくてもフィクスチャで試せる
whisky-ai watch disable 3
```

### macOS で自動実行する（launchd）

```bash
REPO_DIR="$(pwd)"   # リポジトリの絶対パス
sed "s#REPO_DIR#$REPO_DIR#g" deploy/macos/com.whisky-ai.watch.plist \
  > ~/Library/LaunchAgents/com.whisky-ai.watch.plist
mkdir -p data/logs
launchctl load ~/Library/LaunchAgents/com.whisky-ai.watch.plist
launchctl start com.whisky-ai.watch          # すぐに 1 回試す
tail data/logs/watch-*.log
```

毎日 7:17 と 19:17 に起動します。API を呼ぶのは間隔（既定 24 時間）が過ぎた対象だけです。
止めるときは `launchctl unload ~/Library/LaunchAgents/com.whisky-ai.watch.plist` を実行します。
Mac がスリープ中だった回は、起動後に実行されます。

## 開発

```bash
pytest          # テスト
ruff check src tests
mypy            # strict
```

## データについての注意

* `sample_data.py` の蒸留所・銘柄・JAN・価格は **すべて架空** です。
  JAN は GS1 のインストアコード領域（先頭が `2`）を使っているので、実在商品の JAN とは衝突しません。
* 実在商品のマスターデータは含めていません。追加する場合は、公式サイトなどの出典を `sources` に登録したうえで行い、未確認の値は入れないでください。
* API で取得したデータは、各社の規約に従って手元でのみ保存してください（[API 調査メモ](docs/api_research.md)）。
