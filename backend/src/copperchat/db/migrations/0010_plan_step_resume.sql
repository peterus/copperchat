-- v0.5.0 #13 (spec §9.2): plan_revise from a running step. The step pauses (new step
-- status 'paused') and its budget counters live on its row, so they survive the new
-- run that approval starts and a server restart; a revision keeps the revision it
-- replaced (JSON) so that rejecting it falls back instead of ending the plan.

-- The status CHECK gains 'paused': SQLite cannot alter a CHECK, so the table is rebuilt.
CREATE TABLE plan_steps_new (
    id                TEXT PRIMARY KEY,
    plan_id           TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    idx               INTEGER NOT NULL,
    title             TEXT NOT NULL,
    intent            TEXT NOT NULL,
    expected_changes  INTEGER NOT NULL,
    touches           TEXT NOT NULL,      -- JSON {refs, nets}
    verification      TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN
                      ('pending', 'running', 'done', 'failed', 'interrupted', 'paused')),
    run_id            TEXT NULL,
    checkpoint_id     TEXT NULL,
    change_count      INTEGER NOT NULL DEFAULT 0,
    summary           TEXT NULL,
    failure           TEXT NULL,
    erc_result_id     TEXT NULL,
    step_used         INTEGER NOT NULL DEFAULT 0,   -- logical changes used so far
    step_placed       TEXT NOT NULL DEFAULT '[]',   -- JSON: symbols placed in the step
    -- changes.id before the step's fresh begin: its change count spans every run since
    step_change_base  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (plan_id, idx)
);
INSERT INTO plan_steps_new(id, plan_id, idx, title, intent, expected_changes, touches,
    verification, status, run_id, checkpoint_id, change_count, summary, failure,
    erc_result_id)
SELECT id, plan_id, idx, title, intent, expected_changes, touches, verification, status,
    run_id, checkpoint_id, change_count, summary, failure, erc_result_id FROM plan_steps;
DROP TABLE plan_steps;
ALTER TABLE plan_steps_new RENAME TO plan_steps;
-- A step's change count (plan_step_done) filters by step and row id.
CREATE INDEX IF NOT EXISTS ix_changes_step ON changes(project_id, plan_step_id, id);
-- Begun, unfinished steps (the ones that resume with their base) keep today's count:
-- the rows of their last run, or none yet. Pending / failed steps begin afresh anyway.
UPDATE plan_steps SET step_change_base = COALESCE(
    (SELECT MIN(c.id) - 1 FROM changes c
     WHERE c.project_id = (SELECT p.project_id FROM plans p WHERE p.id = plan_steps.plan_id)
       AND c.plan_step_id = plan_steps.id AND c.run_id = plan_steps.run_id),
    (SELECT MAX(id) FROM changes), 0)
WHERE status IN ('running', 'interrupted') AND run_id IS NOT NULL;

ALTER TABLE plans ADD COLUMN revise_reason TEXT NULL;
-- The paused revision a proposed revision replaced (JSON {revision, pause_reason,
-- revise_reason, steps}); NULL when rejecting the plan ends it.
ALTER TABLE plans ADD COLUMN previous TEXT NULL;
