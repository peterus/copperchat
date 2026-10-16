CREATE TABLE messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        TEXT NOT NULL,
    client_message_id TEXT NULL,
    run_id            TEXT NULL,
    role              TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    kind              TEXT NOT NULL CHECK (kind IN ('text', 'thinking', 'tool_use', 'tool_result', 'image', 'error', 'notice')),
    content           TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL CHECK (status IN ('queued', 'streaming', 'done', 'cancelled', 'error')),
    metadata          TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_messages_client ON messages(project_id, client_message_id)
    WHERE client_message_id IS NOT NULL;
CREATE INDEX ix_messages_project_id ON messages(project_id, id);
CREATE INDEX ix_messages_project_status ON messages(project_id, status);

CREATE TABLE runs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT NULL,
    reason      TEXT NULL,
    usage       TEXT NULL
);
CREATE INDEX ix_runs_project ON runs(project_id, started_at);

CREATE TABLE agent_sessions (
    project_id     TEXT PRIMARY KEY,
    sdk_session_id TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE rate_limit_state (
    project_id TEXT PRIMARY KEY,
    resets_at  TEXT NULL
);
