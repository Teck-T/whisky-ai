-- 0001_initial: 正規化商品マスター / 照合 / 価格 / 香味特徴の初期スキーマ
--
-- PostgreSQL 移行を見据えた規約 (docs/design.md 「PostgreSQL 移行」参照):
--   * 主キーは INTEGER PRIMARY KEY (PG では BIGINT GENERATED ALWAYS AS IDENTITY)
--   * 日時は UTC の ISO8601 文字列 'YYYY-MM-DDTHH:MM:SSZ' (PG では TIMESTAMPTZ)
--   * 真偽値は 0/1 + CHECK (PG では BOOLEAN)
--   * JSON は TEXT + json_valid CHECK (PG では JSONB)
--   * 金額は円単位 INTEGER、度数は 1/100 % 単位 INTEGER (43.0% = 4300) で浮動小数を避ける
--   * SQLite 固有の構文 (AUTOINCREMENT, WITHOUT ROWID, 型なし列) は使わない

-- 情報源。定価・実在商品マスター・API取得データなど、すべての事実はここに出典を持つ。
CREATE TABLE sources (
    id            INTEGER PRIMARY KEY,
    kind          TEXT    NOT NULL CHECK (kind IN (
                      'official_site', 'press_release', 'api', 'retailer_page',
                      'manual_entry', 'sample')),
    title         TEXT    NOT NULL,
    url           TEXT,
    publisher     TEXT,
    retrieved_at  TEXT    NOT NULL,
    note          TEXT
);

CREATE TABLE distilleries (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    name_ja     TEXT,
    country     TEXT    NOT NULL,
    region      TEXT,
    status      TEXT    NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'closed', 'silent', 'unknown')),
    source_id   INTEGER NOT NULL REFERENCES sources(id),
    created_at  TEXT    NOT NULL,
    UNIQUE (name, country)
);

-- 銘柄 (ブランド)。ブレンデッドは複数蒸留所を持つため蒸留所とは多対多。
CREATE TABLE brands (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE,
    name_ja      TEXT,
    owner        TEXT,
    category     TEXT    NOT NULL CHECK (category IN (
                     'single_malt', 'blended_malt', 'blended', 'single_grain',
                     'grain', 'bourbon', 'rye', 'other')),
    source_id    INTEGER NOT NULL REFERENCES sources(id),
    created_at   TEXT    NOT NULL
);

CREATE TABLE brand_distilleries (
    brand_id       INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    distillery_id  INTEGER NOT NULL REFERENCES distilleries(id),
    role           TEXT    NOT NULL DEFAULT 'producer'
                           CHECK (role IN ('producer', 'component', 'bottler')),
    PRIMARY KEY (brand_id, distillery_id)
);

-- リリース (エクスプレッション)。例: 「12年」「シェリーカスク 2024 エディション」
CREATE TABLE releases (
    id             INTEGER PRIMARY KEY,
    brand_id       INTEGER NOT NULL REFERENCES brands(id),
    name           TEXT    NOT NULL,
    name_ja        TEXT,
    age_years      INTEGER CHECK (age_years IS NULL OR age_years BETWEEN 0 AND 100),
    vintage_year   INTEGER,
    edition        TEXT,
    cask_type      TEXT,
    is_limited     INTEGER NOT NULL DEFAULT 0 CHECK (is_limited IN (0, 1)),
    has_batches    INTEGER NOT NULL DEFAULT 0 CHECK (has_batches IN (0, 1)),
    source_id      INTEGER NOT NULL REFERENCES sources(id),
    created_at     TEXT    NOT NULL,
    UNIQUE (brand_id, name)
);

-- バッチ / ロット / カスク番号。度数がバッチごとに異なる場合はボトル側で表す。
CREATE TABLE batches (
    id             INTEGER PRIMARY KEY,
    release_id     INTEGER NOT NULL REFERENCES releases(id),
    batch_code     TEXT    NOT NULL,          -- 正規化済み (例: '3', 'A2024')
    batch_label    TEXT,                      -- 表示用 (例: 'Batch No.3')
    bottling_year  INTEGER,
    cask_number    TEXT,
    outturn        INTEGER,                   -- 本数
    source_id      INTEGER NOT NULL REFERENCES sources(id),
    created_at     TEXT    NOT NULL,
    UNIQUE (release_id, batch_code)
);

-- ボトル = 物理的な SKU (リリース × バッチ × 容量 × 度数 × 包装)。商品照合の到達点。
CREATE TABLE bottles (
    id           INTEGER PRIMARY KEY,
    release_id   INTEGER NOT NULL REFERENCES releases(id),
    batch_id     INTEGER REFERENCES batches(id),
    volume_ml    INTEGER NOT NULL CHECK (volume_ml > 0),
    abv_bp       INTEGER NOT NULL CHECK (abv_bp BETWEEN 0 AND 10000),  -- 43.0% = 4300
    packaging    TEXT    NOT NULL DEFAULT 'standard',  -- 'standard', 'gift_box', 'no_box' 等
    display_name TEXT    NOT NULL,
    source_id    INTEGER NOT NULL REFERENCES sources(id),
    created_at   TEXT    NOT NULL
);
-- batch_id が NULL の行も一意にするため COALESCE を使った式インデックス (PG も同構文で可)
CREATE UNIQUE INDEX ux_bottles_spec
    ON bottles (release_id, COALESCE(batch_id, 0), volume_ml, abv_bp, packaging);

-- JAN/EAN/UPC。同一 JAN がバッチ違いで再利用される現実があるため一意制約は (type, value, bottle)。
CREATE TABLE product_identifiers (
    id          INTEGER PRIMARY KEY,
    bottle_id   INTEGER NOT NULL REFERENCES bottles(id) ON DELETE CASCADE,
    id_type     TEXT    NOT NULL CHECK (id_type IN ('jan13', 'jan8', 'upc12', 'itf14')),
    value       TEXT    NOT NULL,
    source_id   INTEGER NOT NULL REFERENCES sources(id),
    created_at  TEXT    NOT NULL,
    UNIQUE (id_type, value, bottle_id)
);
CREATE INDEX ix_product_identifiers_value ON product_identifiers (value);

-- 表記ゆれ辞書 (日本語名・英語名・略称)。
CREATE TABLE aliases (
    id           INTEGER PRIMARY KEY,
    entity_type  TEXT    NOT NULL CHECK (entity_type IN ('distillery', 'brand', 'release')),
    entity_id    INTEGER NOT NULL,
    alias        TEXT    NOT NULL,
    normalized   TEXT    NOT NULL,
    lang         TEXT,
    UNIQUE (entity_type, entity_id, normalized)
);
CREATE INDEX ix_aliases_normalized ON aliases (normalized);

-- メーカー希望小売価格 (MSRP)。出典必須 (source_id NOT NULL)。実勢価格とは別テーブル。
CREATE TABLE retail_price_references (
    id              INTEGER PRIMARY KEY,
    bottle_id       INTEGER NOT NULL REFERENCES bottles(id),
    price_jpy       INTEGER NOT NULL CHECK (price_jpy > 0),
    tax_included    INTEGER NOT NULL CHECK (tax_included IN (0, 1)),
    price_type      TEXT    NOT NULL DEFAULT 'msrp'
                            CHECK (price_type IN ('msrp', 'open_price_announced')),
    effective_from  TEXT,
    effective_to    TEXT,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    created_at      TEXT    NOT NULL
);

-- ===== 販売側 (マーケットプレイス) =====

CREATE TABLE shops (
    id               INTEGER PRIMARY KEY,
    marketplace      TEXT    NOT NULL CHECK (marketplace IN ('rakuten', 'yahoo', 'mock', 'manual')),
    external_shop_id TEXT    NOT NULL,
    name             TEXT    NOT NULL,
    url              TEXT,
    created_at       TEXT    NOT NULL,
    UNIQUE (marketplace, external_shop_id)
);

-- 販売商品 (出品)。外部のタイトル・JAN・URL を生のまま保持し、照合結果は bottle_id/match_status。
CREATE TABLE listings (
    id                 INTEGER PRIMARY KEY,
    marketplace        TEXT    NOT NULL CHECK (marketplace IN ('rakuten', 'yahoo', 'mock', 'manual')),
    external_item_id   TEXT    NOT NULL,
    shop_id            INTEGER REFERENCES shops(id),
    title              TEXT    NOT NULL,
    item_url           TEXT,
    jan_raw            TEXT,
    parsed_volume_ml   INTEGER,
    parsed_abv_bp      INTEGER,
    parsed_age_years   INTEGER,
    parsed_batch_code  TEXT,
    bottle_id          INTEGER REFERENCES bottles(id),
    match_status       TEXT    NOT NULL DEFAULT 'unprocessed' CHECK (match_status IN (
                           'unprocessed', 'matched', 'pending_review', 'unmatched', 'rejected')),
    match_method       TEXT,   -- 'jan', 'name_spec', 'manual'
    match_score        REAL,
    first_seen_at      TEXT    NOT NULL,
    last_seen_at       TEXT    NOT NULL,
    UNIQUE (marketplace, external_item_id),
    CHECK ((match_status = 'matched') = (bottle_id IS NOT NULL))
);
CREATE INDEX ix_listings_bottle ON listings (bottle_id);
CREATE INDEX ix_listings_status ON listings (match_status);

-- 要確認キュー。不確実な照合はここに候補として保存し、自動統合しない。
CREATE TABLE match_candidates (
    id           INTEGER PRIMARY KEY,
    listing_id   INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    bottle_id    INTEGER NOT NULL REFERENCES bottles(id),
    score        REAL    NOT NULL,
    reasons      TEXT    NOT NULL CHECK (json_valid(reasons)),
    status       TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'accepted', 'rejected', 'superseded')),
    created_at   TEXT    NOT NULL,
    decided_at   TEXT,
    decided_by   TEXT,
    UNIQUE (listing_id, bottle_id)
);
CREATE INDEX ix_match_candidates_status ON match_candidates (status);

-- API 生レスポンス (監査・再パース用)。規約に合わせ expires_at で保持期限を管理する。
CREATE TABLE api_raw_responses (
    id            INTEGER PRIMARY KEY,
    marketplace   TEXT    NOT NULL,
    endpoint      TEXT    NOT NULL,
    request_params TEXT   NOT NULL CHECK (json_valid(request_params)),  -- APIキーは保存しない
    status_code   INTEGER NOT NULL,
    body          TEXT,
    fetched_at    TEXT    NOT NULL,
    expires_at    TEXT
);

-- 実勢価格の観測履歴 (追記のみ)。税込/税抜・送料の有無を原値のまま残す。
CREATE TABLE price_observations (
    id                     INTEGER PRIMARY KEY,
    listing_id             INTEGER NOT NULL REFERENCES listings(id),
    observed_at            TEXT    NOT NULL,
    price_jpy              INTEGER NOT NULL CHECK (price_jpy >= 0),   -- 表示価格 (原値)
    price_tax_included     INTEGER CHECK (price_tax_included IN (0, 1)),  -- NULL = 不明
    tax_included_price_jpy INTEGER,  -- 税込価格。算出できない場合 NULL
    shipping_fee_jpy       INTEGER CHECK (shipping_fee_jpy IS NULL OR shipping_fee_jpy >= 0),
    shipping_condition     TEXT    NOT NULL DEFAULT 'unknown' CHECK (shipping_condition IN (
                               'included', 'free', 'conditional_free', 'separate', 'unknown')),
    in_stock               INTEGER CHECK (in_stock IN (0, 1)),          -- NULL = 不明
    source_id              INTEGER NOT NULL REFERENCES sources(id),
    raw_response_id        INTEGER REFERENCES api_raw_responses(id),
    created_at             TEXT    NOT NULL
);
CREATE INDEX ix_price_observations_listing_time ON price_observations (listing_id, observed_at);

-- ===== 香味特徴 (LLM 抽出) =====

CREATE TABLE reviews (
    id           INTEGER PRIMARY KEY,
    release_id   INTEGER REFERENCES releases(id),
    batch_id     INTEGER REFERENCES batches(id),
    bottle_id    INTEGER REFERENCES bottles(id),
    source_id    INTEGER NOT NULL REFERENCES sources(id),
    author       TEXT,
    lang         TEXT,
    body         TEXT    NOT NULL,
    rating       REAL,
    reviewed_at  TEXT,
    created_at   TEXT    NOT NULL,
    CHECK (release_id IS NOT NULL OR batch_id IS NOT NULL OR bottle_id IS NOT NULL)
);

-- LLM 抽出結果 1 回分。JSON 原文 + 再現性のためのモデル/プロンプト版。
CREATE TABLE flavor_extractions (
    id              INTEGER PRIMARY KEY,
    review_id       INTEGER REFERENCES reviews(id) ON DELETE CASCADE,
    release_id      INTEGER REFERENCES releases(id),
    batch_id        INTEGER REFERENCES batches(id),
    bottle_id       INTEGER REFERENCES bottles(id),
    model_name      TEXT    NOT NULL,   -- 例: 'qwen2.5-14b-instruct'
    model_revision  TEXT,
    prompt_version  TEXT    NOT NULL,
    schema_version  TEXT    NOT NULL,
    payload         TEXT    NOT NULL CHECK (json_valid(payload)),
    is_valid        INTEGER NOT NULL CHECK (is_valid IN (0, 1)),
    validation_errors TEXT  CHECK (validation_errors IS NULL OR json_valid(validation_errors)),
    created_at      TEXT    NOT NULL,
    CHECK (review_id IS NOT NULL OR release_id IS NOT NULL
           OR batch_id IS NOT NULL OR bottle_id IS NOT NULL)
);

-- 検索・推薦用に展開した記述子 (JSON から派生。payload が正)。
CREATE TABLE flavor_notes (
    id             INTEGER PRIMARY KEY,
    extraction_id  INTEGER NOT NULL REFERENCES flavor_extractions(id) ON DELETE CASCADE,
    phase          TEXT    NOT NULL CHECK (phase IN ('nose', 'palate', 'finish', 'overall')),
    descriptor     TEXT    NOT NULL,
    category       TEXT,
    intensity      REAL    CHECK (intensity IS NULL OR intensity BETWEEN 0 AND 1)
);
CREATE INDEX ix_flavor_notes_descriptor ON flavor_notes (descriptor);

-- 数値軸 (甘さ・スモーキーさ等 0..1)
CREATE TABLE flavor_axes (
    extraction_id  INTEGER NOT NULL REFERENCES flavor_extractions(id) ON DELETE CASCADE,
    axis           TEXT    NOT NULL,
    value          REAL    NOT NULL CHECK (value BETWEEN 0 AND 1),
    PRIMARY KEY (extraction_id, axis)
);
