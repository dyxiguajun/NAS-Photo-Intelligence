"""Additive Phase 3 tables; historical metadata and binary evidence stay intact."""
V3 = """
CREATE TABLE visual_observations (
 visual_id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(file_id),
 job_id TEXT NOT NULL REFERENCES jobs(job_id), status TEXT NOT NULL CHECK(status IN ('valid','failed','unsupported','limited')),
 pipeline_key TEXT NOT NULL, fingerprint TEXT, before_json TEXT, result_json TEXT,
 pixel_sha256 TEXT, width INTEGER, height INTEGER,
 read_bytes INTEGER NOT NULL CHECK(read_bytes>=0), completed_at TEXT NOT NULL, error_json TEXT,
 reused_visual_id INTEGER REFERENCES visual_observations(visual_id),
 binary_hash_id INTEGER REFERENCES hash_observations(hash_id),
 CHECK(status!='valid' OR (pixel_sha256 IS NOT NULL AND length(pixel_sha256)=64 AND width>0 AND height>0 AND result_json IS NOT NULL AND error_json IS NULL)));
CREATE TABLE visual_current (
 file_id INTEGER PRIMARY KEY REFERENCES files(file_id), visual_id INTEGER REFERENCES visual_observations(visual_id),
 status TEXT NOT NULL CHECK(status IN ('valid','stale','unavailable','verifying','unsupported','limited')),
 checked_at TEXT NOT NULL);
CREATE INDEX visual_pixels ON visual_observations(pipeline_key,pixel_sha256,width,height);
CREATE VIEW active_visual_fingerprints AS
 SELECT v.*,f.source_id,f.relative_path,f.metadata_json,f.fingerprint AS metadata_fingerprint,c.checked_at
 FROM visual_current c JOIN visual_observations v ON v.visual_id=c.visual_id
 JOIN files f ON f.file_id=c.file_id
 WHERE c.status='valid' AND v.status='valid' AND (v.binary_hash_id IS NULL OR EXISTS
 (SELECT 1 FROM hash_current h WHERE h.file_id=v.file_id AND h.hash_id=v.binary_hash_id AND h.status='valid'));
CREATE TABLE visual_comparisons (
 comparison_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id), created_at TEXT NOT NULL,
 parameters_json TEXT NOT NULL, scope_json TEXT NOT NULL, stats_json TEXT NOT NULL);
CREATE TABLE visual_relations (
 relation_id INTEGER PRIMARY KEY, comparison_id TEXT NOT NULL REFERENCES visual_comparisons(comparison_id),
 a_file_id INTEGER NOT NULL REFERENCES files(file_id), b_file_id INTEGER NOT NULL REFERENCES files(file_id),
 a_visual_id INTEGER NOT NULL REFERENCES visual_observations(visual_id), b_visual_id INTEGER NOT NULL REFERENCES visual_observations(visual_id),
 kind TEXT NOT NULL CHECK(kind IN ('pixel_equivalent','visual_similarity_candidate')),
 status TEXT NOT NULL CHECK(status IN ('evidenced','unverified')), evidence_json TEXT NOT NULL,
 UNIQUE(comparison_id,a_file_id,b_file_id,kind), CHECK(a_file_id<b_file_id));
CREATE VIEW active_visual_relations AS
 SELECT r.* FROM visual_relations r JOIN active_visual_fingerprints a ON a.visual_id=r.a_visual_id
 JOIN active_visual_fingerprints b ON b.visual_id=r.b_visual_id;
CREATE TRIGGER invalidate_visual_after_index_change AFTER UPDATE OF fingerprint,status ON files
 WHEN NEW.fingerprint IS NOT OLD.fingerprint OR NEW.status!='ready'
 BEGIN UPDATE visual_current SET status='stale' WHERE file_id=NEW.file_id; END;
CREATE TRIGGER invalidate_visual_after_hash_change AFTER UPDATE OF hash_id,status ON hash_current
 WHEN NEW.hash_id IS NOT OLD.hash_id OR NEW.status!='valid'
 BEGIN UPDATE visual_current SET status='stale' WHERE file_id=NEW.file_id; END;
INSERT INTO schema_migrations VALUES(3,strftime('%Y-%m-%dT%H:%M:%fZ','now'),'Phase 3: versioned pixel/perceptual evidence and pairwise candidates');
PRAGMA user_version=3;
"""
