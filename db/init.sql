-- Users table
CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username    VARCHAR(64) UNIQUE NOT NULL,
    email       VARCHAR(255) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login  TIMESTAMPTZ,
    notes       TEXT
);

-- Audit log
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    username    VARCHAR(64),
    action      VARCHAR(64) NOT NULL,
    detail      TEXT,
    ip_address  VARCHAR(45),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS audit_log_username_idx  ON audit_log(username);
CREATE INDEX IF NOT EXISTS audit_log_created_at_idx ON audit_log(created_at);

-- Admin configuration (credentials + system settings)
-- Seeded on first backend startup from environment variables.
-- After that, managed entirely via the admin UI.
CREATE TABLE IF NOT EXISTS admin_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Default rows are inserted by the backend on first start, not here,
-- because the hashed password requires bcrypt which is a Python concern.
