import fcntl
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import PurePosixPath
from . import __version__
from .process import run_worker
from .store import connect, encode, stamp, save_analysis
from .time_rules import analyze


@contextmanager
def exclusive(config):
    path = config.outputs["database"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError("Another index writer is active; retry later")
        yield


def tool_version(config):
    p = subprocess.run([config.exiftool, "-config", "", "-ver"], capture_output=True, text=True, timeout=10)
    if p.returncode: raise ValueError("ExifTool is unavailable")
    return "nas-photo/" + __version__ + ";exiftool/" + p.stdout.strip()


def create_job(config, items):
    if not 1 <= len(items) <= config.limits["max_files"]:
        raise ValueError("File limit exceeded")
    with exclusive(config):
        db = connect(config)
        try:
            job_id = uuid.uuid4().hex
            with db:
                db.execute("INSERT INTO jobs(job_id,status,created_at,updated_at,rules_json,limits_json) VALUES (?,?,?,?,?,?)",
                           (job_id, "queued", stamp(), stamp(), encode(config.rules), encode(config.limits)))
                for item in items:
                    db.execute("""INSERT INTO files(source_id,relative_path,first_seen,last_seen) VALUES (?,?,?,?)
                               ON CONFLICT(source_id,relative_path) DO UPDATE SET last_seen=excluded.last_seen""",
                               (item["source_id"], item["relative_path"], stamp(), stamp()))
                    fid = db.execute("SELECT file_id FROM files WHERE source_id=? AND relative_path=?",
                                     (item["source_id"], item["relative_path"])).fetchone()[0]
                    db.execute("INSERT OR IGNORE INTO job_items(job_id,file_id,prior_json) VALUES (?,?,?)",
                               (job_id, fid, encode(item.get("prior_evidence", {}))))
            return job_id
        finally: db.close()


def summary(db, job_id=None):
    job = dict(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()) if job_id else None
    counts = dict(db.execute("SELECT status,count(*) FROM job_items WHERE job_id=? GROUP BY status", (job_id,)).fetchall()) if job_id else {}
    if job:
        job["rules"] = json.loads(job.pop("rules_json"))
        job["limits"] = json.loads(job.pop("limits_json"))
    labels_query = "SELECT tag,count(*) FROM labels GROUP BY tag" if not job_id else "SELECT l.tag,count(*) FROM labels l JOIN job_items i ON i.file_id=l.file_id WHERE i.job_id=? GROUP BY l.tag"
    labels = dict(db.execute(labels_query, (job_id,) if job_id else ()).fetchall())
    return {"job": job, "items": counts, "file_records": db.execute("SELECT count(*) FROM files").fetchone()[0],
            "observation_records": db.execute("SELECT count(*) FROM observations").fetchone()[0], "labels": labels}


def log(config, event):
    config.outputs["logs"].mkdir(parents=True, exist_ok=True)
    with (config.outputs["logs"] / "events.jsonl").open("a") as f:
        f.write(encode({"at": stamp(), **event}) + "\n")


def relations(db, fid, sid, rel, kind):
    stem = str(PurePosixPath(rel).with_suffix(""))
    # Candidates only; no deduplication, no source crossing, no file modifications.
    for row in db.execute("SELECT file_id,relative_path,metadata_json FROM files WHERE source_id=? AND file_id<>? AND status='ready'", (sid, fid)):
        other = json.loads(row["metadata_json"])
        if other["metadata"]["kind"] != kind and str(PurePosixPath(row["relative_path"]).with_suffix("")) == stem:
            a, b = sorted([fid, row["file_id"]])
            db.execute("INSERT OR IGNORE INTO candidate_relations VALUES (?,?,?,?,'unverified')",
                       (a, b, "live_photo_candidate", encode({"basis": "same source and relative stem; identity not confirmed"})))


def run_job(config, job_id, retry_failed=False):
    with exclusive(config):
        db = connect(config)
        started = time.monotonic()
        final = "interrupted"
        job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job:
            db.close()
            raise ValueError("Unknown job_id")
        if job["kind"] != "metadata":
            db.close()
            raise ValueError("Use the matching duplicates/similarity resume command for this job")
        try:
            limits = json.loads(job["limits_json"])
            rules = json.loads(job["rules_json"])
            extractor = tool_version(config)
            with db:
                db.execute("UPDATE job_items SET status='pending' WHERE job_id=? AND status='running'", (job_id,))
                if retry_failed:
                    db.execute("UPDATE job_items SET status='pending',attempts=0 WHERE job_id=? AND status='failed'", (job_id,))
                db.execute("UPDATE jobs SET status='running',updated_at=? WHERE job_id=?", (stamp(), job_id))
            while True:
                remaining = limits["run_budget_seconds"] - (time.monotonic() - started)
                if remaining <= 0: break
                item = db.execute("""SELECT i.*,f.source_id,f.relative_path,f.fingerprint,f.extractor,
                                  f.metadata_json FROM job_items i JOIN files f ON i.file_id=f.file_id
                                  WHERE i.job_id=? AND i.status='pending' ORDER BY i.item_id LIMIT 1""", (job_id,)).fetchone()
                if item is None:
                    failed = db.execute("SELECT count(*) FROM job_items WHERE job_id=? AND status='failed'", (job_id,)).fetchone()[0]
                    final = "completed_with_errors" if failed else "completed"
                    break
                if item["attempts"] >= limits["max_attempts"]:
                    error = {"code": "attempt_limit", "message": "previous interrupted attempts exhausted the retry budget; explicit --retry-failed required"}
                    with db:
                        db.execute("UPDATE job_items SET status='failed',last_error_json=? WHERE item_id=?", (encode(error), item["item_id"]))
                        db.execute("INSERT INTO errors(job_id,file_id,occurred_at,attempt,code,message) VALUES (?,?,?,?,?,?)",
                                   (job_id, item["file_id"], stamp(), item["total_attempts"], error["code"], error["message"]))
                    continue
                with db:
                    db.execute("UPDATE job_items SET status='running',attempts=attempts+1,total_attempts=total_attempts+1 WHERE item_id=?", (item["item_id"],))
                source = config.sources.get(item["source_id"])
                if not source: raise ValueError("Job source missing from current config")
                request = {"root": source["root"], "relative_path": item["relative_path"],
                           "max_file_bytes": limits["max_file_bytes"], "extractor": extractor,
                           "previous_fingerprint": item["fingerprint"], "previous_extractor": item["extractor"],
                           "exiftool": config.exiftool, "ffprobe": config.ffprobe,
                           "tool_timeout": min(limits["file_timeout_seconds"], remaining),
                           "prior_evidence": json.loads(item["prior_json"])}
                response = run_worker(request, min(limits["file_timeout_seconds"], remaining))
                with db:
                    if not response["ok"]:
                        error = response["error"]
                        attempt = item["attempts"] + 1
                        status = "pending" if attempt < limits["max_attempts"] else "failed"
                        db.execute("UPDATE job_items SET status=?,last_error_json=? WHERE item_id=?", (status, encode(error), item["item_id"]))
                        db.execute("UPDATE files SET status='read_error',last_error_json=? WHERE file_id=?", (encode(error), item["file_id"]))
                        db.execute("INSERT INTO errors(job_id,file_id,occurred_at,attempt,code,message) VALUES (?,?,?,?,?,?)",
                                   (job_id, item["file_id"], stamp(), item["total_attempts"] + 1, error["code"], error["message"]))
                        outcome = {"status": status, "error": error}
                    else:
                        result = response["result"]
                        unchanged = result["unchanged"] and item["metadata_json"] is not None
                        if unchanged:
                            saved = json.loads(item["metadata_json"])
                            # New manifest evidence may add historical observations, without replacing original raw data.
                            prior = json.loads(item["prior_json"])
                            votes = saved["format_votes"]
                            if prior.get("format_votes"):
                                from .metadata import canonical
                                votes = [v for v in votes if not v.get("historical")]
                                votes += [{"tool": k, "format": canonical(v), "historical": True} for k, v in prior["format_votes"].items()]
                                saved = {**saved, "format_votes": votes, "prior_evidence": prior}
                            analysis = analyze(result["fs"], saved["metadata"], votes, rules, item["relative_path"])
                        else:
                            saved = result
                            analysis = analyze(result["fs"], result["metadata"], result["format_votes"], rules, item["relative_path"])
                            db.execute("INSERT INTO observations(file_id,job_id,observed_at,extractor,raw_json,metadata_json,analysis_json) VALUES (?,?,?,?,?,?,?)",
                                       (item["file_id"], job_id, stamp(), extractor, encode(result["raw"]), encode(result), encode(analysis)))
                        db.execute("""UPDATE files SET fingerprint=?,extractor=?,fs_json=?,metadata_json=?,status='ready',
                                   last_error_json=NULL,last_seen=?,last_indexed=? WHERE file_id=?""",
                                   (result["fingerprint"], extractor, encode(result["fs"]), encode(saved), stamp(), stamp(), item["file_id"]))
                        save_analysis(db, item["file_id"], analysis)
                        db.execute("UPDATE job_items SET status=?,last_error_json=NULL WHERE item_id=?", ("skipped" if unchanged else "done", item["item_id"]))
                        relations(db, item["file_id"], item["source_id"], item["relative_path"], saved["metadata"]["kind"])
                        outcome = {"status": "skipped" if unchanged else "done"}
                log(config, {"job_id": job_id, "file_id": item["file_id"], **outcome})
        except KeyboardInterrupt:
            final = "cancelled"
        finally:
            with db:
                db.execute("UPDATE job_items SET status='pending' WHERE job_id=? AND status='running'", (job_id,))
                db.execute("UPDATE jobs SET status=?,updated_at=?,elapsed_seconds=elapsed_seconds+? WHERE job_id=?",
                           (final, stamp(), time.monotonic() - started, job_id))
            result_summary = summary(db, job_id)
            db.close()
        return result_summary


def recalculate(config):
    with exclusive(config):
        db = connect(config)
        try:
            with db:
                for row in db.execute("SELECT * FROM files WHERE metadata_json IS NOT NULL"):
                    saved = json.loads(row["metadata_json"])
                    analysis = analyze(json.loads(row["fs_json"]), saved["metadata"], saved["format_votes"], config.rules, row["relative_path"])
                    save_analysis(db, row["file_id"], analysis)
        finally: db.close()
