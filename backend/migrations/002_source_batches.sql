-- 来源整月更正：范围版本表与来源行归属列。
-- 新装环境由 SQLAlchemy metadata 建表；已有数据卷升级时执行本迁移（可重复执行）。
CREATE TABLE IF NOT EXISTS source_scopes (
    id SERIAL PRIMARY KEY,
    channel VARCHAR NOT NULL,
    month VARCHAR NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    checksum VARCHAR NOT NULL DEFAULT '',
    updated_at VARCHAR NOT NULL,
    CONSTRAINT source_scope_unique UNIQUE (channel, month)
);

ALTER TABLE source_rows ADD COLUMN IF NOT EXISTS batch_key VARCHAR;
ALTER TABLE source_rows ADD COLUMN IF NOT EXISTS scope_version INTEGER NOT NULL DEFAULT 1;
UPDATE source_rows SET scope_version = 1 WHERE scope_version IS NULL;

CREATE TABLE IF NOT EXISTS source_batches (
    id SERIAL PRIMARY KEY,
    batch_key VARCHAR NOT NULL UNIQUE,
    channel VARCHAR NOT NULL,
    month VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'preview',
    scope_version INTEGER NOT NULL,
    scope_checksum VARCHAR NOT NULL,
    row_count INTEGER NOT NULL,
    rows JSON NOT NULL,
    rows_checksum VARCHAR NOT NULL DEFAULT '',
    preview JSON NOT NULL DEFAULT '{}',
    created_by VARCHAR NOT NULL,
    created_at VARCHAR NOT NULL,
    applied_by VARCHAR,
    applied_at VARCHAR,
    applied_checksum VARCHAR
);

CREATE INDEX IF NOT EXISTS source_batches_scope_idx ON source_batches (channel, month, status);
CREATE INDEX IF NOT EXISTS source_rows_scope_idx ON source_rows (channel, day);
CREATE INDEX IF NOT EXISTS source_rows_batch_idx ON source_rows (batch_key);

-- 已有来源的初始范围版本按实际行登记；没有来源行的范围留待首次更正时创建。
INSERT INTO source_scopes (channel, month, version, checksum, updated_at)
SELECT channel, substr(day, 1, 7), 1, '', to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS')
FROM source_rows
GROUP BY channel, substr(day, 1, 7)
ON CONFLICT (channel, month) DO NOTHING;
