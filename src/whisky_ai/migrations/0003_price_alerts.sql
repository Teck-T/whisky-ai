-- 0003_price_alerts: 値下がり通知のルールと発火履歴

CREATE TABLE price_alert_rules (
    id                INTEGER PRIMARY KEY,
    bottle_id         INTEGER NOT NULL REFERENCES bottles(id),
    kind              TEXT    NOT NULL CHECK (kind IN ('target_price', 'drop_pct')),
    target_price_jpy  INTEGER CHECK (target_price_jpy IS NULL OR target_price_jpy > 0),
    drop_pct          REAL    CHECK (drop_pct IS NULL OR (drop_pct > 0 AND drop_pct < 100)),
    lookback_days     INTEGER NOT NULL DEFAULT 30 CHECK (lookback_days >= 1),
    enabled           INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    -- 条件を満たさない状態を一度見たら 1 (再び満たしたら通知できる)。通知したら 0
    armed             INTEGER NOT NULL DEFAULT 1 CHECK (armed IN (0, 1)),
    last_fired_price_jpy INTEGER,
    last_fired_at     TEXT,
    created_at        TEXT    NOT NULL,
    CHECK ((kind = 'target_price') = (target_price_jpy IS NOT NULL)),
    CHECK ((kind = 'drop_pct') = (drop_pct IS NOT NULL))
);

CREATE TABLE price_alert_events (
    id                  INTEGER PRIMARY KEY,
    rule_id             INTEGER NOT NULL REFERENCES price_alert_rules(id) ON DELETE CASCADE,
    bottle_id           INTEGER NOT NULL REFERENCES bottles(id),
    listing_id          INTEGER NOT NULL REFERENCES listings(id),
    price_jpy           INTEGER NOT NULL,       -- 税込 (送料別)
    reference_price_jpy INTEGER,                -- 比較した基準 (目標価格 または 相場の中央値)
    message             TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    notified_channels   TEXT                    -- 実際に送れた通知先 (カンマ区切り)
);
CREATE INDEX ix_price_alert_events_rule ON price_alert_events (rule_id, created_at);
