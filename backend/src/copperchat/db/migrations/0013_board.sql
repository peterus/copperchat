-- SP4 board checkpoints and their evidence (board plan index contract B-C10, spec §8.3).
-- board_checkpoints: one row per user approval; only the latest row per (project_id,
-- board_file, stage) counts. board_reviews: one row per piece of evidence (a pcb_render or a
-- pcb_check) with both hashes of the state it was produced for.
CREATE TABLE board_checkpoints (id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, board_file TEXT NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('placement','board')), hash TEXT NOT NULL,
  conversation_id TEXT NOT NULL, run_id TEXT NOT NULL, user_quote TEXT NOT NULL, approved_at TEXT NOT NULL);
CREATE INDEX board_checkpoints_latest ON board_checkpoints(project_id, board_file, stage, id);
CREATE TABLE board_reviews (id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, board_file TEXT NOT NULL,
  conversation_id TEXT, stage TEXT, placement_hash TEXT NOT NULL, board_hash TEXT NOT NULL, run_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('render','check')), view TEXT, check_level TEXT CHECK (check_level IN ('quick','full')),
  hard_failures INTEGER, m1_pass INTEGER, created_at TEXT NOT NULL);
CREATE INDEX board_reviews_placement ON board_reviews(project_id, board_file, placement_hash);
CREATE INDEX board_reviews_board ON board_reviews(project_id, board_file, board_hash);
