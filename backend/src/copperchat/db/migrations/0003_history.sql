-- Phase 7: per-turn snapshots and the document hashes recorded after CopperChat's own saves.
CREATE TABLE IF NOT EXISTS history_snapshots (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    label       TEXT NOT NULL,
    files       TEXT NOT NULL            -- JSON array of rel posix paths
);
CREATE INDEX IF NOT EXISTS ix_history_project ON history_snapshots(project_id, created_at);
CREATE TABLE IF NOT EXISTS document_hashes (
    project_id  TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    PRIMARY KEY (project_id, rel_path)
);
