-- v0.5.0 (sch_* addressability, #12): ERC justifications of a plan step, kept across
-- the retries of its plan_step_done. Plain TEXT step id without a foreign key: plan
-- saves rewrite plan_steps rows (same ids), which would cascade into this table.
CREATE TABLE IF NOT EXISTS erc_step_justifications (
    plan_step_id    TEXT PRIMARY KEY,
    justifications  TEXT NOT NULL,   -- JSON object: violation id -> reason
    rules           TEXT NOT NULL,   -- JSON array: {match: {type?, sheet?}, reason}
    updated_at      TEXT NOT NULL
);

-- Pattern rules of a finished step with the number of errors each covered (plan card).
ALTER TABLE erc_results ADD COLUMN rules TEXT NOT NULL DEFAULT '[]';
