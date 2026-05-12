-- FTP Activity Log Schema
-- Applied automatically on first start via docker-entrypoint-initdb.d

-- Named user downloads
CREATE TABLE IF NOT EXISTS user_downloads (
    id          BIGSERIAL PRIMARY KEY,
    logged_at   TIMESTAMPTZ NOT NULL,
    ip_address  INET NOT NULL,
    username    TEXT NOT NULL,
    filepath    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    bytes       BIGINT
);

CREATE INDEX IF NOT EXISTS idx_ud_logged_at  ON user_downloads (logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_ud_username   ON user_downloads (username);
CREATE INDEX IF NOT EXISTS idx_ud_ip         ON user_downloads (ip_address);
CREATE INDEX IF NOT EXISTS idx_ud_user_time  ON user_downloads (username, logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_ud_filename   ON user_downloads (filename);

-- Anonymous downloads
CREATE TABLE IF NOT EXISTS anon_downloads (
    id          BIGSERIAL PRIMARY KEY,
    logged_at   TIMESTAMPTZ NOT NULL,
    ip_address  INET NOT NULL,
    filepath    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    bytes       BIGINT
);

CREATE INDEX IF NOT EXISTS idx_ad_logged_at  ON anon_downloads (logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_ad_ip         ON anon_downloads (ip_address);
CREATE INDEX IF NOT EXISTS idx_ad_filename   ON anon_downloads (filename);

-- Tailer config / status (written by logtailer, read by backend)
-- Stored here so logtailer can persist its state and the UI can read it
-- without adding a dependency on the main user DB.
CREATE TABLE IF NOT EXISTS tailer_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed defaults (logtailer will overwrite these as it runs)
INSERT INTO tailer_config (key, value) VALUES
    ('log_filename',          'full_user.log'),
    ('log_retention_days',    '90'),
    ('log_retention_enabled', 'true'),
    ('tailer_status',         'starting'),
    ('tailer_last_write',     ''),
    ('tailer_pos',            '0'),
    ('tailer_total_rows',     '0')
ON CONFLICT (key) DO NOTHING;
