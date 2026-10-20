-- Sub-project 3 (sessions & design docs), spec §4.2 / plan S-C1. Independent of 0008:
-- conversation_id is plain TEXT without a foreign key.

-- Full-content revisions of docs/design/** (spec §7.6).
CREATE TABLE IF NOT EXISTS doc_revisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         TEXT NOT NULL,
    path               TEXT NOT NULL,             -- relative, e.g. docs/design/requirements.md
    version            TEXT NOT NULL,             -- body version of `content` (§7.3)
    prev_version       TEXT NULL,
    added              INTEGER NOT NULL DEFAULT 0, -- line counts vs prev, computed at write
    removed            INTEGER NOT NULL DEFAULT 0,
    bytes              INTEGER NOT NULL,          -- for the per-project storage cap
    author             TEXT NOT NULL CHECK (author IN ('agent', 'user', 'external')),
    conversation_id    TEXT NULL,
    run_id             TEXT NULL,
    summary            TEXT NOT NULL DEFAULT '',
    conversation_label TEXT NULL,                 -- e.g. 'Session 2 — Schematic', at write time
    content            TEXT NOT NULL,             -- full file text incl. frontmatter
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_doc_revisions ON doc_revisions(project_id, path, id);

-- Approval is DB state (§7.3); the frontmatter status only mirrors it.
CREATE TABLE IF NOT EXISTS doc_approvals (
    project_id   TEXT NOT NULL,
    path         TEXT NOT NULL,
    body_version TEXT NOT NULL,
    approved_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, path)
);
