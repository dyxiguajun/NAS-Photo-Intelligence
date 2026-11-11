"""Versioned, additive migrations; old evidence is never rewritten."""
from contextlib import closing
import sqlite3

V2 = """
ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'metadata';
ALTER TABLE jobs ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE jobs ADD COLUMN read_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN read_budget_used INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN accounting_complete INTEGER NOT NULL DEFAULT 1;
ALTER TABLE jobs ADD COLUMN cache_saved_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE job_items ADD COLUMN reservation_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE job_items ADD COLUMN progress_path TEXT;
ALTER TABLE job_items ADD COLUMN read_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE job_items ADD COLUMN accounting_complete INTEGER NOT NULL DEFAULT 1;
CREATE TABLE hash_observations (
 hash_id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(file_id),
 job_id TEXT NOT NULL REFERENCES jobs(job_id), algorithm TEXT NOT NULL CHECK(algorithm='sha256'),
 implementation TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('valid','failed')),
 digest TEXT, size INTEGER, fingerprint TEXT, before_json TEXT, after_json TEXT,
 read_bytes INTEGER NOT NULL CHECK(read_bytes>=0), completed_at TEXT, error_json TEXT,
 CHECK(status!='valid' OR (length(digest)=64 AND size=read_bytes AND completed_at IS NOT NULL AND error_json IS NULL)));
CREATE TABLE hash_current (
 file_id INTEGER PRIMARY KEY REFERENCES files(file_id), hash_id INTEGER REFERENCES hash_observations(hash_id),
 status TEXT NOT NULL CHECK(status IN ('valid','stale','unavailable','verifying')),
 checked_at TEXT NOT NULL);
CREATE INDEX hash_digest ON hash_observations(algorithm,digest,size);
CREATE VIEW active_exact_hashes AS
 SELECT h.*,f.source_id,f.relative_path,f.metadata_json,f.fs_json,f.fingerprint AS metadata_fingerprint,
 c.checked_at FROM hash_current c JOIN hash_observations h ON h.hash_id=c.hash_id
 JOIN files f ON f.file_id=c.file_id WHERE c.status='valid' AND h.status='valid';
CREATE VIEW exact_duplicate_groups AS
 SELECT algorithm,digest,size,count(*) AS member_count FROM active_exact_hashes
 GROUP BY algorithm,digest,size HAVING count(*)>1;
CREATE VIEW exact_duplicate_members AS
 SELECT h.* FROM active_exact_hashes h JOIN exact_duplicate_groups g
 ON h.algorithm=g.algorithm AND h.digest=g.digest AND h.size=g.size;
CREATE TRIGGER invalidate_hash_after_index_change AFTER UPDATE OF fingerprint,status ON files
 WHEN NEW.fingerprint IS NOT OLD.fingerprint OR NEW.status!='ready'
 BEGIN UPDATE hash_current SET status='stale' WHERE file_id=NEW.file_id; END;
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL,description TEXT NOT NULL);
INSERT INTO schema_migrations VALUES(2,strftime('%Y-%m-%dT%H:%M:%fZ','now'),'Phase 1 -> 2: exact hash evidence, task I/O accounting, dynamic groups');
PRAGMA user_version=2;
"""


def migrate(db, path, backup=True):
    from .visual_migration import V3
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (1, 2, 3): raise ValueError("Migration requires schema 1, 2 or 3")
    while version < 3:
        if backup:
            destination = path.with_name(path.stem + f".pre-phase{version + 1}.sqlite3")
            if not destination.exists():
                with closing(sqlite3.connect(destination)) as copy: db.backup(copy)
            with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as copy:
                if (copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                        or copy.execute("PRAGMA user_version").fetchone()[0] != version):
                    raise ValueError("Pre-migration backup is invalid; migration aborted")
        try:
            db.executescript("BEGIN IMMEDIATE;\n" + (V2 if version == 1 else V3) + "\nCOMMIT;")
        except BaseException:
            if db.in_transaction: db.rollback()
            raise
        version += 1
