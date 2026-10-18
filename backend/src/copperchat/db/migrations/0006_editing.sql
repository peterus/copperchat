-- Sub-project 2 (editing): turn snapshots vs. plan-step checkpoints.
ALTER TABLE history_snapshots ADD COLUMN kind TEXT NOT NULL DEFAULT 'turn'
    CHECK (kind IN ('turn', 'step'));
ALTER TABLE history_snapshots ADD COLUMN plan_step_id TEXT NULL;

-- Per-project editing settings (defaults apply while no row exists).
CREATE TABLE IF NOT EXISTS editing_settings (
    project_id        TEXT PRIMARY KEY,
    plan_policy       TEXT NOT NULL DEFAULT 'large'
                      CHECK (plan_policy IN ('always', 'large', 'never')),
    change_budget     INTEGER NOT NULL DEFAULT 20,
    annotation_block  INTEGER NOT NULL DEFAULT 100 CHECK (annotation_block IN (100, 1000)),
    show_agent_view   INTEGER NOT NULL DEFAULT 1 CHECK (show_agent_view IN (0, 1))
);

-- Change journal: one row per item a mutating tool added, modified or deleted.
CREATE TABLE IF NOT EXISTS changes (
    id            INTEGER PRIMARY KEY,
    project_id    TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    plan_step_id  TEXT NULL,
    call_seq      INTEGER NOT NULL,
    tool          TEXT NOT NULL,
    op            TEXT NOT NULL CHECK (op IN ('add', 'modify', 'delete')),
    kind          TEXT NOT NULL,
    uuid          TEXT NOT NULL,
    sheet_path    TEXT NOT NULL,
    ref           TEXT NULL,
    net           TEXT NULL,
    before        TEXT NULL,             -- JSON
    after         TEXT NULL,             -- JSON
    side_effect   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_changes_run ON changes(project_id, run_id);

-- ERC delta of a turn or plan step against its baseline snapshot.
CREATE TABLE IF NOT EXISTS erc_results (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL,
    run_id                TEXT NOT NULL,
    plan_step_id          TEXT NULL,
    baseline_snapshot_id  TEXT,
    new_errors            TEXT NOT NULL,  -- JSON array
    new_warnings          TEXT NOT NULL,  -- JSON array
    resolved              TEXT NOT NULL,  -- JSON array
    justifications        TEXT NOT NULL,  -- JSON object
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_erc_results_run ON erc_results(project_id, run_id);

-- Agent plans; at most one open (proposed/approved/running/paused) plan per project.
CREATE TABLE IF NOT EXISTS plans (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    title             TEXT NOT NULL,
    summary           TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN
                      ('proposed', 'approved', 'running', 'paused', 'done', 'rejected', 'aborted')),
    revision          INTEGER NOT NULL,
    event_seq         INTEGER NOT NULL DEFAULT 0,
    pause_reason      TEXT NULL,
    base_snapshot_id  TEXT NULL,
    created_run_id    TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_plans_open ON plans(project_id)
    WHERE status IN ('proposed', 'approved', 'running', 'paused');

-- Ordered steps of a plan, deleted with it.
CREATE TABLE IF NOT EXISTS plan_steps (
    id                TEXT PRIMARY KEY,
    plan_id           TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    idx               INTEGER NOT NULL,
    title             TEXT NOT NULL,
    intent            TEXT NOT NULL,
    expected_changes  INTEGER NOT NULL,
    touches           TEXT NOT NULL,      -- JSON {refs, nets}
    verification      TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN
                      ('pending', 'running', 'done', 'failed', 'interrupted')),
    run_id            TEXT NULL,
    checkpoint_id     TEXT NULL,
    change_count      INTEGER NOT NULL DEFAULT 0,
    summary           TEXT NULL,
    failure           TEXT NULL,
    erc_result_id     TEXT NULL,
    UNIQUE (plan_id, idx)
);
