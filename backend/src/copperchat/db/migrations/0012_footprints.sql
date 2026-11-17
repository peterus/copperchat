-- Footprint tools (FP-C7): stat columns for hash reuse, content-addressed blob refs,
-- the footprint write journal and footprint reviews.
ALTER TABLE document_hashes ADD COLUMN size INTEGER;      -- NULL = never reused
ALTER TABLE document_hashes ADD COLUMN mtime_ns INTEGER;
ALTER TABLE document_hashes ADD COLUMN inode INTEGER;
CREATE TABLE history_blob_refs (
  project_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, sha256 TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, sha256));
CREATE INDEX history_blob_refs_project ON history_blob_refs(project_id, sha256);
CREATE TABLE footprint_writes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL, run_id TEXT, lib_id TEXT NOT NULL,
  file_rel TEXT NOT NULL,           -- repo-root-relative POSIX path of the .kicad_mod / fp-lib-table
  op TEXT NOT NULL CHECK (op IN ('row_add','create','replace','approve','delete')),
  state TEXT NOT NULL CHECK (state IN ('pending','done','compensated','leftover')),
  pre_sha256 TEXT, post_s2_sha256 TEXT, post_sha256 TEXT,   -- sha hex or 'absent'
  geometry_hash TEXT, created_at TEXT NOT NULL, finalised_at TEXT);
CREATE INDEX footprint_writes_project ON footprint_writes(project_id, state, created_at);
CREATE INDEX footprint_writes_file ON footprint_writes(file_rel, id);
CREATE TABLE footprint_reviews (
  conversation_id TEXT NOT NULL, lib_id TEXT NOT NULL, geometry_hash TEXT NOT NULL,
  run_id TEXT, rendered_at TEXT, checked_at TEXT, check_errors INTEGER,
  PRIMARY KEY (conversation_id, lib_id, geometry_hash));
