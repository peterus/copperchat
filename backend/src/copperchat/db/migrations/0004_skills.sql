-- Phase 8: per-project skill toggles and chat attachments.
CREATE TABLE skill_prefs (
    project_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    PRIMARY KEY (project_id, skill_name)
);

CREATE TABLE uploads (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'pdf')),
    name TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_uploads_project ON uploads(project_id);
