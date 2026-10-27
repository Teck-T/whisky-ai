# 楽天市場 / Yahoo!ショッピング 商品検索 API 調査メモ

調査日: 2026-10-27。公式ドキュメントの Web ページを取得して確認した。
**「未確認」と書いた項目は推測で埋めていない。** PR 2 に着手する前に、アプリ登録画面と規約の原文で確認すること。

## 1. 楽天市場商品検索 API（Rakuten Web Service）

| 項目 | 内容 | 確認状況 |
|---|---|---|
| 最新版 | `IchibaItem/Search` **2026-07-01** 版（前版は 2026-04-01） | 公式ドキュメントで確認 |
| エンドポイント | `https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701` | 公式ドキュメントで確認 |
| 認証 | `applicationId` と `accessKey` の **両方が必須**。`accessKey` はヘッダーとクエリパラメータのどちらでも送れる | 公式ドキュメントで確認 |
| アフィリエイト | `affiliateId` は任意。指定すると `affiliateUrl` / `shopAffiliateUrl` が返る | 公式ドキュメントで確認 |
| 検索条件 | `keyword` / `genreId` / `itemCode` / `shopCode` のどれか 1 つが必須 | 公式ドキュメントで確認 |
| 旧 API | 旧ドメイン `app.rakuten.co.jp` 系は 2026-05-13 に廃止され、アプリの再登録が必要だったと報告されている | **二次情報のみ**（個人開発者の記事） |
| Referer | 新 API は Referer ヘッダーを検証し、ないと 403 になるという報告がある | **二次情報のみ** |
| レート制限 | 具体的な数値はドキュメントに記載がない。超過すると HTTP 429（`number of allowed requests has been exceeded`）。同じ URL への短時間の集中アクセスは一時的に応答しなくなる | 公式（数値は**未確認**） |
| 主なレスポンス項目 | `itemPrice`、`taxFlag`（0=税込 / 1=税抜）、`postageFlag`（0=送料込 / 1=送料別）、`availability`（0=在庫なし / 1=あり）、`shopName`、`shopCode`、`itemUrl`、`itemCode`（`shop:1234` 形式） | 公式ドキュメントで確認 |
| JAN | 商品検索 API のレスポンス項目に JAN は **ない** | 公式ドキュメントで確認 |
| 送料額 | `postageFlag` で送料込か別かはわかるが、**送料の金額は返らない** | 公式ドキュメントで確認 |

### 規約（楽天ウェブサービス利用規約）

確認したのは英訳版（法的効力なし。原文は日本語版）。

* 第 10 条 1 項 9 号: API で得た情報を **共有可能な場所に保存すること** は禁止。
  → DB は手元の Mac で個人利用する前提にする。DB ファイルを第三者に配布したり、共有ストレージに置いたりしない。
* 第 10 条 1 項 7 号: 得た情報の利用は、楽天が定める目的の範囲に限られる。
* 第 10 条 1 項 4 号: 楽天アフィリエイト以外の方法で収益を得ることは禁止（許可がある場合を除く）。
* 第 10 条 1 項 6 号: 楽天グループと競合するサービスの提供は禁止。
  → 価格比較サービスとして公開する場合は、事前に規約の原文を確認し、必要なら楽天に問い合わせること。
* 第 13 条: 楽天が求める場合はクレジット表示が必要（方法は別途定め）。
* 第 7 条 3 項: アクセス回数・時間・頻度を制限されることがある。
* **データの保持期間を定めた条文は見当たらなかった**（未確認。原文で再確認する）。

### 設計への反映

* `taxFlag=0` → `price_tax_included=1`。`taxFlag=1` → 税込額は推計せず NULL。
* `postageFlag=0` → `shipping_condition='included'`。`1` → `'separate'`（送料額は NULL）。
* JAN が返らないので、楽天の出品は **名前 + 仕様** で照合する。照合が不確実なものは要確認キューに入る。
* レート制限の数値が不明なため、PR 2 では **1 リクエスト/秒** をデフォルトにし、429 を受けたら指数バックオフする。
* 共有保存の禁止に合わせ、`api_raw_responses` に `expires_at` を持たせ、定期的に削除できるようにした。

## 2. Yahoo!ショッピング 商品検索 API v3

| 項目 | 内容 | 確認状況 |
|---|---|---|
| エンドポイント | `https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch`（JSON） | 公式ドキュメントで確認 |
| 認証 | `appid`（Yahoo!デベロッパーネットワークで登録した Client ID） | 公式ドキュメントで確認 |
| JAN 検索 | `jan_code` パラメータあり。レスポンスにも `hits/janCode` がある（空のこともある） | 公式ドキュメントで確認 |
| レート制限 | **1 クエリ/秒**。同じ URL に短時間で大量にアクセスすると一定時間使えなくなる。429 を返すことがある。`start + results` は 1,000 まで | 公式ドキュメントで確認 |
| 価格 | `hits/price`、`hits/taxExcludePrice`、`hits/premiumPrice`、`hits/priceLabel/*`（`taxable`、`defaultPrice`、`discountedPrice`、`fixedPrice` など） | 公式ドキュメントで確認 |
| 送料 | `hits/shipping/code`（1=設定なし / 2=送料無料 / 3=条件付き送料無料）と `name`。**送料の金額は返らない** | 公式ドキュメントで確認 |
| 在庫 | `hits/inStock`（真偽値） | 公式ドキュメントで確認 |
| 販売店 | `hits/seller/sellerId`、`name`、`url` | 公式ドキュメントで確認 |
| URL | `hits/url` | 公式ドキュメントで確認 |
| アフィリエイト | バリューコマースのアフィリエイトプログラムを利用できる | 公式で確認 |
| 旧仕様 | 商品検索 v1 は「旧仕様」。v3 を使う | 公式で確認 |

### 規約・ガイドライン

* Yahoo!デベロッパーネットワークのガイドライン（特約事項）と LINEヤフー共通利用規約への同意が必要。
* 特約事項 第 1 項: クレジット表示ガイドラインとクレジット配置ルールを守る必要がある。
  * 表示文言は「Webサービス by Yahoo! JAPAN」などの指定 4 種類から選ぶ。指定の HTML は改変禁止。アプリケーションの下部に配置する。
  * 守らないと API の利用を停止されることがある。
* 特約事項 第 2 項: 開発したソフトウェアに、当社が定めるアプリケーション ID を付ける。
* **データの保存・キャッシュ期間について明記した箇所は、確認した範囲には見当たらなかった**（未確認。
  共通利用規約の第 2 章と「利用制限について」ページを原文で確認する）。
* `fixedPrice` は名前から定価のように見えるが、出品者が入力した値と考えられ、**メーカー希望小売価格の出典としては扱わない**
  （§5 の方針。`retail_price_references` には登録しない）。

### 設計への反映

* `priceLabel.taxable` と `taxExcludePrice` から税込 / 税抜を判断する。両方の金額が返る場合はどちらも保存できる。
* `shipping.code` は 2 → `free`、3 → `conditional_free`、1 → `unknown`。送料額は NULL。
* `janCode` が空でなければ `listings.jan_raw` に入れ、JAN で照合する。チェックディジットが不正な値は無視する（照合時の理由に記録）。
* PR 2 では 1 リクエスト/秒を守るトークンバケットを使う（楽天と同じ実装）。

## 3. 共通の運用ルール（PR 2 で実装）

1. API キーは環境変数だけで扱う（`.env.example`）。ログや DB（`request_params`）には残さない。
2. 1 リクエスト/秒以下に抑え、429 を受けたら指数バックオフする。同じクエリの短時間の再取得は避ける。
3. 取得した生データは手元の DB にだけ保存し、共有しない。`expires_at` で期限が来たら削除する。
4. 画面やレポートで表示する場合は、各社のクレジット表示ルールに従う。
5. 公開サービスにする場合は、規約の原文（特に楽天第 10 条の競合禁止と収益化の制限）を再確認する。

## 出典

* 楽天ウェブサービス「楽天市場商品検索API (version:2026-07-01)」 https://webservice.rakuten.co.jp/documentation/ichiba-item-search （2026-10-27 取得）
* 楽天ウェブサービス 利用規約（英訳版） https://webservice.rakuten.co.jp/guide/rule （2026-10-27 取得）
* Yahoo!デベロッパーネットワーク「商品検索（v3）」 https://developer.yahoo.co.jp/webapi/shopping/v3/itemsearch.html （2026-10-27 取得）
* Yahoo!デベロッパーネットワーク「ショッピングWeb API」 https://developer.yahoo.co.jp/webapi/shopping/ （2026-10-27 取得）
* Yahoo!デベロッパーネットワーク ガイドライン https://developer.yahoo.co.jp/guideline/ ／ クレジット表示 https://developer.yahoo.co.jp/attribution/ （2026-10-27 取得）
* 二次情報（旧 API の廃止と Referer）: https://qiita.com/StreamSolty/items/2a2a203cbc37fefdc115 、 https://zenn.dev/milky/scraps/252296c031eb4b
