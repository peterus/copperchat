-- Sub-project 3 (sessions): several conversations per project (spec §4.2, plan S-C1).
CREATE TABLE conversations (
    id              TEXT PRIMARY KEY,              -- 32 hex chars (uuid4 hex)
    project_id      TEXT NOT NULL,
    seq             INTEGER NOT NULL,              -- 1, 2, 3 ... per project ("Session 3")
    title           TEXT NOT NULL,                 -- default "Session <seq>"
    type            TEXT NOT NULL
                    CHECK (type IN ('free', 'requirements', 'schematic', 'board')),
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closing', 'closed')),
    archived        INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    sdk_session_id  TEXT NULL,                     -- replaces agent_sessions
    docs_seen       TEXT NOT NULL DEFAULT '{}',    -- JSON {path: version}
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_message_at TEXT NULL,
    closed_at       TEXT NULL,
    UNIQUE (project_id, seq)
);
CREATE INDEX ix_conversations_project ON conversations(project_id, archived, last_message_at);

-- Existing history -> "Session 1" (type free) for every project that has messages, runs
-- or a stored SDK session.
INSERT INTO conversations (id, project_id, seq, title, type, sdk_session_id,
                           created_at, updated_at, last_message_at)
SELECT lower(hex(randomblob(16))), p.project_id, 1, 'Session 1', 'free',
       (SELECT sdk_session_id FROM agent_sessions a WHERE a.project_id = p.project_id),
       COALESCE((SELECT MIN(created_at) FROM messages m WHERE m.project_id = p.project_id),
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
       strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
       (SELECT MAX(created_at) FROM messages m WHERE m.project_id = p.project_id)
FROM (SELECT project_id FROM messages UNION SELECT project_id FROM runs
      UNION SELECT project_id FROM agent_sessions) p;

-- Nullable: SQLite cannot add a NOT NULL column with a foreign key and no default. The
-- repos set it on every insert; after this backfill no NULL is left.
ALTER TABLE messages ADD COLUMN conversation_id TEXT NULL REFERENCES conversations(id);
ALTER TABLE runs     ADD COLUMN conversation_id TEXT NULL REFERENCES conversations(id);
ALTER TABLE plans    ADD COLUMN conversation_id TEXT NULL REFERENCES conversations(id);
UPDATE messages SET conversation_id =
    (SELECT id FROM conversations c WHERE c.project_id = messages.project_id);
UPDATE runs SET conversation_id =
    (SELECT id FROM conversations c WHERE c.project_id = runs.project_id);
UPDATE plans SET conversation_id =
    (SELECT id FROM conversations c WHERE c.project_id = plans.project_id);
CREATE INDEX ix_messages_conversation ON messages(conversation_id, id);
CREATE INDEX ix_runs_conversation ON runs(conversation_id, started_at);

DROP TABLE agent_sessions;
