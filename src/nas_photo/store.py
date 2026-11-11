import json
import sqlite3
from datetime import datetime, timezone


def stamp(): return datetime.now(timezone.utc).isoformat()
def encode(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 source_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, logical_root TEXT NOT NULL,
 description TEXT, last_access_root TEXT NOT NULL, UNIQUE(device_id,logical_root));
CREATE TABLE IF NOT EXISTS files (
 file_id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
 relative_path TEXT NOT NULL, fingerprint TEXT, extractor TEXT,
 fs_json TEXT, metadata_json TEXT, dimensions_json TEXT,
 status TEXT NOT NULL DEFAULT 'pending', last_error_json TEXT,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, last_indexed TEXT,
 UNIQUE(source_id,relative_path));
CREATE TABLE IF NOT EXISTS jobs (
 job_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 rules_json TEXT NOT NULL, limits_json TEXT NOT NULL, elapsed_seconds REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS job_items (
 item_id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id),
 file_id INTEGER NOT NULL REFERENCES files(file_id), prior_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 total_attempts INTEGER NOT NULL DEFAULT 0, last_error_json TEXT,
 UNIQUE(job_id,file_id));
CREATE TABLE IF NOT EXISTS observations (
 observation_id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(file_id),
 job_id TEXT NOT NULL REFERENCES jobs(job_id), observed_at TEXT NOT NULL, extractor TEXT NOT NULL,
 raw_json TEXT NOT NULL, metadata_json TEXT NOT NULL, analysis_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS time_evidence (
 evidence_id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(file_id),
 field TEXT NOT NULL, origin TEXT NOT NULL, raw_value TEXT, parsed_value TEXT,
 utc_value TEXT, timezone_state TEXT, error TEXT, evidence_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS labels (
 file_id INTEGER NOT NULL REFERENCES files(file_id), tag TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'engine',
 reasons_json TEXT NOT NULL, calculated_at TEXT NOT NULL, PRIMARY KEY(file_id,tag,origin));
CREATE TABLE IF NOT EXISTS errors (
 error_id INTEGER PRIMARY KEY, job_id TEXT REFERENCES jobs(job_id), file_id INTEGER REFERENCES files(file_id),
 occurred_at TEXT NOT NULL, attempt INTEGER NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidate_relations (
 a_file_id INTEGER NOT NULL REFERENCES files(file_id), b_file_id INTEGER NOT NULL REFERENCES files(file_id),
 kind TEXT NOT NULL, evidence_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unverified',
 PRIMARY KEY(a_file_id,b_file_id,kind));
CREATE INDEX IF NOT EXISTS item_queue ON job_items(job_id,status,item_id);
CREATE INDEX IF NOT EXISTS label_tag ON labels(tag);
PRAGMA user_version=1;
"""


def connect(config):
    path = config.outputs["database"]
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3):
            db.close(); raise ValueError("Unsupported database schema version")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        if version == 0:
            db.executescript(SCHEMA)
        from .migrations import migrate
        migrate(db, path, backup=version in (1, 2))
        with db:
            for s in config.sources.values():
                old = db.execute("SELECT * FROM sources WHERE source_id=?", (s["source_id"],)).fetchone()
                if old and (old["device_id"], old["logical_root"]) != (s["device_id"], s["logical_root"]):
                    raise ValueError("Existing source_id cannot be reassigned to another logical source")
                db.execute("""INSERT INTO sources VALUES (?,?,?,?,?) ON CONFLICT(source_id)
                           DO UPDATE SET description=excluded.description,last_access_root=excluded.last_access_root""",
                           (s["source_id"], s["device_id"], s["logical_root"], s.get("description"), s["root"]))
        return db
    except BaseException:
        db.close()
        raise


def save_analysis(db, file_id, analysis):
    db.execute("DELETE FROM labels WHERE file_id=? AND origin='engine'", (file_id,))
    for tag, reasons in analysis["labels"].items():
        db.execute("INSERT INTO labels VALUES (?,?, 'engine',?,?)", (file_id, tag, encode(reasons), stamp()))
    db.execute("DELETE FROM time_evidence WHERE file_id=?", (file_id,))
    for t in analysis["times"]:
        db.execute("""INSERT INTO time_evidence(file_id,field,origin,raw_value,parsed_value,utc_value,
                   timezone_state,error,evidence_json) VALUES (?,?,?,?,?,?,?,?,?)""",
                   (file_id, t["field"], t["origin"], t["raw"], t["parsed"], t["utc"], t["timezone"], t["error"], encode(t)))
    db.execute("UPDATE files SET dimensions_json=? WHERE file_id=?", (encode(analysis["dimensions"]), file_id))
