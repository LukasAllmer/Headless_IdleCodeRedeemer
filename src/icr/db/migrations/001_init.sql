-- Initial schema. See PLAN.md §3.

CREATE TABLE accounts (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL UNIQUE,
    user_id       TEXT    NOT NULL,
    user_hash     TEXT    NOT NULL,
    instance_id   TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    credentials_ok INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    UNIQUE (user_id, user_hash)
);

CREATE TABLE codes (
    id            INTEGER PRIMARY KEY,
    code          TEXT    NOT NULL UNIQUE,
    source        TEXT    NOT NULL,
    source_ref    TEXT,
    first_seen_at TEXT    NOT NULL,
    note          TEXT
);

CREATE TABLE redemptions (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    code_id          INTEGER NOT NULL REFERENCES codes (id) ON DELETE CASCADE,
    status           TEXT    NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 1,
    first_attempt_at TEXT    NOT NULL,
    last_attempt_at  TEXT    NOT NULL,
    loot_json        TEXT,
    error            TEXT,
    UNIQUE (account_id, code_id)
);

CREATE INDEX idx_redemptions_status ON redemptions (status);
CREATE INDEX idx_redemptions_account ON redemptions (account_id);

CREATE TABLE kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
