CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    rel_path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    last_opened_at TEXT
);

-- Server-side login sessions: the cookie carries only a signed session id, so
-- logout (row deleted) invalidates the cookie even if a copy of it survives.
CREATE TABLE auth_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
