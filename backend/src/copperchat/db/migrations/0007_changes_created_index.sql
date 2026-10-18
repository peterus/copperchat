-- E10 review: ChangeRepo.since / since_stats (every /changes/diff request) filter by
-- project and time; without this index they scan all of the project's change rows.
CREATE INDEX IF NOT EXISTS ix_changes_created ON changes(project_id, created_at);
