# LLM（ローカル Qwen）との受け渡し

このリポジトリは LLM を動かさない。LLM 側（Mac 上の Qwen 系モデル）とは、次の 2 つのファイルでやり取りする。

| 方向 | ファイル | 作り方 |
|---|---|---|
| こちら → LLM | 出力の型（JSON Schema） | `whisky-ai flavor schema > flavor_schema.json` |
| LLM → こちら | 抽出結果（JSONL） | LLM 側のバッチ処理が書き出し、`whisky-ai flavor import` で取り込む |

## 1. JSON Schema を LLM に渡す

構造化出力（文法制約付き生成）に対応したランタイムなら、スキーマを渡すと出力が必ずこの形になる。
いずれもこのリポジトリでは動作確認していないので、各ランタイムのドキュメントで最新の指定方法を確認すること。

* llama.cpp（`llama-server` / `llama-cli`）: `--json-schema` オプション、またはサーバーの `json_schema` パラメータ
* Ollama: API の `format` にスキーマ（JSON オブジェクト）を渡す
* MLX / LM Studio: 各ツールの structured output 機能

スキーマの主な制約:

* `schema_version` は `"1"` 固定
* `nose` / `palate` / `finish` は必須（空配列可）、`overall` は任意。要素は `descriptor`（必須）・`category`・`intensity`（0〜1）
* `axes` は必須。使える軸は次の 12 個だけで、値は 0〜1:
  `sweetness`, `smokiness`, `peatiness`, `fruitiness`, `floral`, `spiciness`,
  `woodiness`, `maltiness`, `sherry`, `body`, `richness`, `finish_length`
* `summary`（文字列）と `confidence`（0〜1）は任意

## 2. JSONL の形式（1 行 1 件）

```json
{"target": {"release_id": 12},
 "review": {"body": "レビュー本文", "source_title": "出典名", "url": "https://...", "author": null, "lang": "ja"},
 "model_name": "qwen2.5-32b-instruct", "model_revision": null,
 "prompt_version": "flavor-v1",
 "payload": {"schema_version": "1", "nose": [...], "palate": [...], "finish": [...], "axes": {...}}}
```

* `target` は `release_id` / `batch_id` / `bottle_id` のどれか（ID は `whisky-ai` の DB のもの）。
  バッチやボトル単位の差を区別したいときは、より細かい ID を使う。
* `review` は任意。付けると元のレビュー本文も保存する。`source_kind` は
  `official_site` / `press_release` / `retailer_page` / `manual_entry`（既定）/ `sample` から選ぶ。
  **第三者のレビュー本文を保存する場合は、そのサイトの利用規約と著作権に注意する**（本文は保存せず、出典 URL だけにする選択肢もある）。
* `payload` は LLM の出力そのもの（オブジェクトか JSON 文字列）。不正でも `is_valid=0` で保存されるので、
  プロンプト改善の材料になる。
* `model_name` / `model_revision` / `prompt_version` は、あとで抽出結果を比べるために必ず入れる。

## 3. 取り込み後

```bash
whisky-ai flavor import extractions.jsonl   # {"valid": n, "invalid": n, "skipped": n}
whisky-ai flavor show 12
whisky-ai recommend --like 12
```

不正な出力の確認（SQLite）:

```sql
SELECT id, model_name, prompt_version, validation_errors
FROM flavor_extractions WHERE is_valid = 0 ORDER BY id DESC LIMIT 20;
```
