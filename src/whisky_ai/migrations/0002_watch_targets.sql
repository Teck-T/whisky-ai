-- 0002_watch_targets: 定期収集の監視対象

CREATE TABLE watch_targets (
    id                  INTEGER PRIMARY KEY,
    bottle_id           INTEGER REFERENCES bottles(id),  -- NULL 可 (キーワードだけの監視)
    label               TEXT    NOT NULL,
    marketplace         TEXT    NOT NULL CHECK (marketplace IN ('rakuten', 'yahoo')),
    keyword             TEXT,
    jan                 TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    min_interval_hours  INTEGER NOT NULL DEFAULT 24 CHECK (min_interval_hours >= 1),
    last_run_at         TEXT,
    last_status         TEXT    CHECK (last_status IS NULL OR last_status IN ('ok', 'error')),
    last_error          TEXT,
    created_at          TEXT    NOT NULL,
    CHECK (keyword IS NOT NULL OR jan IS NOT NULL),
    -- 楽天の商品検索 API には JAN 検索がない
    CHECK (marketplace = 'yahoo' OR keyword IS NOT NULL)
);
CREATE UNIQUE INDEX ux_watch_targets_query
    ON watch_targets (marketplace, COALESCE(keyword, ''), COALESCE(jan, ''));
