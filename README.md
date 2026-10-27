# whisky-ai

ウイスキー推薦とオンライン価格監視のためのデータ基盤（Python 3.13 + SQLite）。

ローカル LLM（Qwen 系）がレビューから抽出した香味特徴 JSON と、商品・価格データを永続化します。
LLM の学習と推論はこのリポジトリの範囲外です。

## ドキュメント

* [設計書](docs/design.md): データモデル、商品同一性判定、価格の区別、今後の課題
* [ER図](docs/er_diagram.md)（Mermaid）
* [楽天 / Yahoo! API 調査メモ](docs/api_research.md): 最新仕様、利用条件、データ保存条件

## 現在の機能（PR 1）

* 蒸留所 → 銘柄 → リリース → バッチ → ボトル（容量・度数）→ 販売商品 を区別した正規化スキーマ
* JAN/EAN（チェックディジット検証）、商品名、容量、度数、熟成年数、バッチ番号による同一商品の照合
* 不確実な照合は自動統合せず、要確認キュー（`match_candidates`）に保存し、CLI で承認または却下
* 価格履歴（税込 / 税抜、送料、在庫、取得日時、販売店、URL、情報源）のスキーマと書き込み API
* メーカー希望小売価格は別テーブルに保存し、出典がないと登録できない
* LLM 香味特徴 JSON の検証と保存（不正な出力も原文を保存する）

価格収集モジュール（楽天 / Yahoo! API クライアント、モック）は次の PR で追加します（[設計書 §7](docs/design.md#7-価格収集pr-2の構成)）。

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
| `RAKUTEN_APPLICATION_ID` / `RAKUTEN_ACCESS_KEY` / `RAKUTEN_REFERER` | 楽天 API（PR 2 で使用） |
| `YAHOO_CLIENT_ID` | Yahoo!ショッピング API（PR 2 で使用） |

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
```

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
