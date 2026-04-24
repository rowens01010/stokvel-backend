-- =====================================================================
-- Stokvel — Supabase / Postgres schema
-- Run ONCE in: Supabase Dashboard → SQL Editor → New query → paste → Run
-- =====================================================================

CREATE TABLE IF NOT EXISTS users (
    user_id            TEXT PRIMARY KEY,
    email              TEXT UNIQUE NOT NULL,
    name               TEXT NOT NULL DEFAULT '',
    picture            TEXT NOT NULL DEFAULT '',
    tokens             INTEGER NOT NULL DEFAULT 0,
    diamonds           INTEGER NOT NULL DEFAULT 0,
    is_premium         BOOLEAN NOT NULL DEFAULT false,
    premium_until      TIMESTAMPTZ,
    votes_since_token  INTEGER NOT NULL DEFAULT 0,
    referral_code      TEXT UNIQUE,
    referred_by        TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_sessions (
    session_token TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_sessions_user_id_idx ON user_sessions(user_id);

CREATE TABLE IF NOT EXISTS cards (
    card_id           TEXT PRIMARY KEY,
    owner_id          TEXT NOT NULL,
    owner_name        TEXT NOT NULL DEFAULT '',
    image_url         TEXT NOT NULL,
    smart_link        TEXT NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    votes             INTEGER NOT NULL DEFAULT 0,
    is_premium        BOOLEAN NOT NULL DEFAULT false,
    diamond_boosted   BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS cards_owner_id_idx    ON cards(owner_id);
CREATE INDEX IF NOT EXISTS cards_expires_at_idx  ON cards(expires_at);

CREATE TABLE IF NOT EXISTS votes (
    vote_id     TEXT PRIMARY KEY,
    voter_id    TEXT NOT NULL,
    card_id     TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS votes_card_id_idx  ON votes(card_id);
CREATE INDEX IF NOT EXISTS votes_voter_id_idx ON votes(voter_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    m_payment_id TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'subscription',
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ
);

-- We use the SERVICE_ROLE key from FastAPI, which bypasses RLS anyway.
-- Disabling RLS keeps things explicit.
ALTER TABLE users         DISABLE ROW LEVEL SECURITY;
ALTER TABLE user_sessions DISABLE ROW LEVEL SECURITY;
ALTER TABLE cards         DISABLE ROW LEVEL SECURITY;
ALTER TABLE votes         DISABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions DISABLE ROW LEVEL SECURITY;
