-- Email source: per-account codes, and the mailboxes they arrive in.
--
-- Additive only. Nothing is dropped, rebuilt or rewritten, so an existing
-- database keeps every row it had. `codes.account_id` arrives NULL on every
-- existing row, and NULL means "redeem for every account" -- which is exactly
-- what those codes already meant.

ALTER TABLE codes ADD COLUMN account_id INTEGER REFERENCES accounts (id) ON DELETE CASCADE;

CREATE INDEX idx_codes_account ON codes (account_id);

-- One IMAP mailbox, tied to the game account it is subscribed for.
--
-- Newsletter codes are single-use, so a code that arrives here belongs to that
-- account and no other. The tie is NOT NULL and cascades: an account's personal
-- codes and mailboxes are meaningless once the account is gone.
CREATE TABLE mailboxes (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL UNIQUE,
    account_id      INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    -- 'password' (plain IMAP) or 'microsoft' (XOAUTH2, `secret` is a refresh token)
    auth            TEXT    NOT NULL,
    host            TEXT    NOT NULL,
    port            INTEGER NOT NULL DEFAULT 993,
    username        TEXT    NOT NULL,
    secret          TEXT,
    oauth_client_id TEXT,
    oauth_tenant    TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_polled_at  TEXT,
    last_error      TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX idx_mailboxes_account ON mailboxes (account_id);
