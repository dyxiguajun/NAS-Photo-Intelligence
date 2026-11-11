"""Exact duplicate jobs extend the existing durable jobs and file identities."""
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid
from .engine import exclusive, log
from .hashing import IMPLEMENTATION
from .process import run_worker
from .store import connect, encode, stamp


def readonly(config):
    db = sqlite3.connect(config.outputs["database"].as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def scoped_files(db, items):
    result = []
    for item in items:
        row = db.execute("SELECT * FROM files WHERE source_id=? AND relative_path=?",
                         (item["source_id"], item["relative_path"])).fetchone()
        if row is None or row["fs_json"] is None:
            raise ValueError("Hash scope contains a file without an existing metadata index")
        row = dict(row)
        row["indexed_size"] = json.loads(row["fs_json"])["size"]
        if not isinstance(row["indexed_size"], int) or row["indexed_size"] < 0:
            raise ValueError("Invalid indexed file size")
        result.append(row)
    return result


def candidates(config, items=None):
    db = readonly(config)
    try:
        if items is None:
            groups = [dict(r) for r in db.execute("""SELECT json_extract(fs_json,'$.size') AS size,count(*) AS members
                      FROM files WHERE fs_json IS NOT NULL GROUP BY size HAVING count(*)>1 ORDER BY size LIMIT 200""")]
            return {"scope": "existing index; no content/stat reads", "size_groups": groups,
                    "group_list_limit": 200, "indexed_files": db.execute("SELECT count(*) FROM files").fetchone()[0]}
        rows = scoped_files(db, items)
        sizes = Counter(r["indexed_size"] for r in rows)
        groups = [{"size": size, "members": count,
                   "file_ids": [r["file_id"] for r in rows if r["indexed_size"] == size]}
                  for size, count in sorted(sizes.items()) if count > 1]
        return {"scope": "explicit manifest", "indexed_files": len(rows), "size_groups": groups,
                "candidate_files": sum(g["members"] for g in groups), "content_bytes_read": 0,
                "non_candidates_are_not_proven_unique": True}
    finally: db.close()


def options(config, force=False, include_non_candidates=False, max_read_bytes=None, chunk_bytes=None):
    result = {"max_read_bytes": 64 * 1024 * 1024, "chunk_bytes": 1024 * 1024,
              **config.raw.get("hash_limits", {}), "force_rehash": bool(force),
              "include_non_candidates": bool(include_non_candidates)}
    if max_read_bytes is not None: result["max_read_bytes"] = max_read_bytes
    if chunk_bytes is not None: result["chunk_bytes"] = chunk_bytes
    if type(result["max_read_bytes"]) is not int or not 0 <= result["max_read_bytes"] <= 2 * 1024**3:
        raise ValueError("Read budget must be 0..2 GiB and persists across resume/retries")
    if type(result["chunk_bytes"]) is not int or not 1 <= result["chunk_bytes"] <= 4 * 1024**2:
        raise ValueError("Chunk size must be 1 byte..4 MiB")
    return result


def create_hash_job(config, items, opts):
    if not 1 <= len(items) <= config.limits["max_files"]:
        raise ValueError("Hash manifest file limit exceeded")
    with exclusive(config):
        db = connect(config)
        try:
            rows = scoped_files(db, items)
            sizes = Counter(r["indexed_size"] for r in rows)
            jid = uuid.uuid4().hex
            with db:
                db.execute("""INSERT INTO jobs(job_id,status,created_at,updated_at,rules_json,limits_json,kind,options_json)
                           VALUES (?,?,?,?,?,?,'hash',?)""", (jid, "queued", stamp(), stamp(), encode(config.rules), encode(config.limits), encode(opts)))
                for row in rows:
                    eligible = opts["include_non_candidates"] or sizes[row["indexed_size"]] > 1
                    db.execute("INSERT INTO job_items(job_id,file_id,prior_json,status) VALUES (?,?,?,?)",
                               (jid, row["file_id"], encode({"indexed_size": row["indexed_size"]}), "pending" if eligible else "not_candidate"))
            return jid
        finally: db.close()


def current_state(db, fid, state):
    db.execute("""INSERT INTO hash_current(file_id,status,checked_at) VALUES (?,?,?)
               ON CONFLICT(file_id) DO UPDATE SET status=excluded.status,checked_at=excluded.checked_at""", (fid, state, stamp()))


def safe_progress(config, value):
    if not value: return None
    p = Path(value)
    if p.parent != config.outputs["cache"] or not p.name.startswith("hash-") or p.is_symlink():
        raise ValueError("Invalid persisted progress-counter path")
    return p


def recover_attempt(db, config, item, record_error=False):
    """Unfinished reservations remain charged, even if the counter is incomplete."""
    p = safe_progress(config, item["progress_path"])
    if not p: return 0
    observed = 0
    try: observed = min(max(0, int(p.read_bytes()[:24])), item["reservation_bytes"])
    except (ValueError, OSError): pass
    complete = int(item["reservation_bytes"] == 0)
    with db:
        db.execute("UPDATE jobs SET read_bytes=read_bytes+?,accounting_complete=min(accounting_complete,?) WHERE job_id=?",
                   (observed, complete, item["job_id"]))
        db.execute("""UPDATE job_items SET read_bytes=read_bytes+?,accounting_complete=min(accounting_complete,?),
                   reservation_bytes=0,progress_path=NULL WHERE item_id=?""", (observed, complete, item["item_id"]))
        if record_error:
            finish_error(db, item, {"code": "interrupted_hash_read", "message": "no digest accepted; observed bytes are a lower bound"},
                         json.loads(db.execute("SELECT limits_json FROM jobs WHERE job_id=?", (item["job_id"],)).fetchone()[0])["max_attempts"], observed)
    p.unlink(missing_ok=True)
    return observed


def finish_error(db, item, error, max_attempts, read_bytes=0, before=None):
    status = "pending" if item["attempts"] < max_attempts else "failed"
    db.execute("UPDATE job_items SET status=?,last_error_json=? WHERE item_id=?", (status, encode(error), item["item_id"]))
    current_state(db, item["file_id"], "unavailable")
    db.execute("INSERT INTO errors(job_id,file_id,occurred_at,attempt,code,message) VALUES (?,?,?,?,?,?)",
               (item["job_id"], item["file_id"], stamp(), item["total_attempts"], error["code"], error["message"]))
    db.execute("""INSERT INTO hash_observations(file_id,job_id,algorithm,implementation,status,read_bytes,
               before_json,error_json) VALUES (?,?,'sha256',?,'failed',?,?,?)""",
               (item["file_id"], item["job_id"], IMPLEMENTATION, read_bytes, encode(before), encode(error)))


def hash_status(db, jid):
    row = db.execute("SELECT * FROM jobs WHERE job_id=? AND kind='hash'", (jid,)).fetchone()
    if row is None: raise ValueError("Unknown hash job_id")
    job = dict(row)
    for key in ["options_json", "limits_json", "rules_json"]: job[key.removesuffix("_json")] = json.loads(job.pop(key))
    counts = dict(db.execute("SELECT status,count(*) FROM job_items WHERE job_id=? GROUP BY status", (jid,)).fetchall())
    logical = sum(json.loads(r[0])["indexed_size"] for r in db.execute("SELECT prior_json FROM job_items WHERE job_id=?", (jid,)))
    successful_bytes = db.execute("SELECT coalesce(sum(read_bytes),0) FROM hash_observations WHERE job_id=? AND status='valid'", (jid,)).fetchone()[0]
    return {"job": job, "items": counts, "scoped_files": sum(counts.values()),
            "scoped_indexed_logical_bytes": logical, "program_read_bytes_observed": job["read_bytes"],
            "read_bytes_are_exact": bool(job["accounting_complete"]),
            "read_budget_charged_bytes": job["read_budget_used"], "cache_saved_content_bytes": job["cache_saved_bytes"],
            "full_hash_read_bytes_observed": job["read_bytes"], "partial_fingerprint_read_bytes": 0,
            "successful_full_hash_read_bytes": successful_bytes,
            "failed_or_incomplete_read_bytes_observed": job["read_bytes"] - successful_bytes,
            "unconfirmed_reserved_read_bytes": job["read_budget_used"] - job["read_bytes"],
            "effective_observed_MiB_per_second": job["read_bytes"] / 1048576 / job["elapsed_seconds"] if job["elapsed_seconds"] else 0,
            "network_transfer_bytes": None, "network_accounting_note": "program reads are not SMB/Tailscale wire traffic"}


def run_hash_job(config, jid, retry_failed=False, worker_fn=None):
    worker_fn = worker_fn or run_worker
    with exclusive(config):
        db = connect(config)
        job = db.execute("SELECT * FROM jobs WHERE job_id=? AND kind='hash'", (jid,)).fetchone()
        if not job:
            db.close(); raise ValueError("Unknown hash job_id")
        opts, limits = json.loads(job["options_json"]), json.loads(job["limits_json"])
        started, final = time.monotonic(), "interrupted"
        try:
            for old in db.execute("SELECT * FROM job_items WHERE job_id=? AND progress_path IS NOT NULL", (jid,)).fetchall():
                recover_attempt(db, config, old, record_error=True)
            with db:
                db.execute("UPDATE job_items SET status='pending' WHERE job_id=? AND status='running'", (jid,))
                if retry_failed: db.execute("UPDATE job_items SET status='pending',attempts=0 WHERE job_id=? AND status='failed'", (jid,))
                db.execute("UPDATE jobs SET status='running',updated_at=? WHERE job_id=?", (stamp(), jid))
            while True:
                remaining = limits["run_budget_seconds"] - (time.monotonic() - started)
                if remaining <= 0: break
                row = db.execute("""SELECT i.*,f.source_id,f.relative_path FROM job_items i JOIN files f ON f.file_id=i.file_id
                                 WHERE i.job_id=? AND i.status='pending' ORDER BY i.item_id LIMIT 1""", (jid,)).fetchone()
                if row is None:
                    failed = db.execute("SELECT count(*) FROM job_items WHERE job_id=? AND status='failed'", (jid,)).fetchone()[0]
                    final = "completed_with_errors" if failed else "completed"; break
                item = dict(row)
                if item["attempts"] >= limits["max_attempts"]:
                    with db:
                        finish_error(db, item, {"code": "attempt_limit", "message": "interrupted retry budget exhausted; use --retry-failed"}, limits["max_attempts"])
                    continue
                item["attempts"] += 1; item["total_attempts"] += 1
                with db:
                    db.execute("UPDATE job_items SET status='running',attempts=?,total_attempts=? WHERE item_id=?",
                               (item["attempts"], item["total_attempts"], item["item_id"]))
                source = config.sources.get(item["source_id"])
                if not source: raise ValueError("Job source missing from config")
                request = {"operation": "hash_probe", "root": source["root"], "relative_path": item["relative_path"]}
                file_started = time.monotonic()
                response = worker_fn(request, min(limits["file_timeout_seconds"], remaining))
                if not response["ok"]:
                    with db: finish_error(db, item, response["error"], limits["max_attempts"])
                    continue
                live = response["result"]
                old = db.execute("""SELECT h.* FROM hash_current c JOIN hash_observations h ON h.hash_id=c.hash_id
                                 WHERE c.file_id=?""", (item["file_id"],)).fetchone()
                if old and old["status"] == "valid" and old["fingerprint"] == live["fingerprint"] and old["implementation"] == IMPLEMENTATION and not opts["force_rehash"]:
                    with db:
                        current_state(db, item["file_id"], "valid")
                        db.execute("UPDATE job_items SET status='skipped',last_error_json=NULL WHERE item_id=?", (item["item_id"],))
                        db.execute("UPDATE jobs SET cache_saved_bytes=cache_saved_bytes+? WHERE job_id=?", (live["fs"]["size"], jid))
                    log(config, {"job_id": jid, "file_id": item["file_id"], "status": "hash_cache_hit", "read_bytes": 0})
                    continue
                with db: current_state(db, item["file_id"], "verifying")
                size = live["fs"]["size"]
                if size > limits["max_file_bytes"]:
                    with db: finish_error(db, item, {"code": "file_size_limit", "message": "hash input exceeds per-file limit"}, limits["max_attempts"])
                    continue
                used = db.execute("SELECT read_budget_used FROM jobs WHERE job_id=?", (jid,)).fetchone()[0]
                if used + size > opts["max_read_bytes"]:
                    with db:
                        db.execute("UPDATE job_items SET status='pending',attempts=attempts-1 WHERE item_id=?", (item["item_id"],))
                    final = "budget_exhausted"; break
                remaining = limits["run_budget_seconds"] - (time.monotonic() - started)
                if remaining <= 0: break
                remaining = min(remaining, limits["file_timeout_seconds"] - (time.monotonic() - file_started))
                if remaining <= 0:
                    with db: finish_error(db, item, {"code": "timeout", "message": "file deadline exhausted during preflight"}, limits["max_attempts"])
                    continue
                config.outputs["cache"].mkdir(parents=True, exist_ok=True)
                fd, progress = tempfile.mkstemp(prefix="hash-", dir=config.outputs["cache"])
                os.write(fd, b"0" * 24); os.close(fd)
                with db:
                    db.execute("UPDATE jobs SET read_budget_used=read_budget_used+? WHERE job_id=?", (size, jid))
                    db.execute("UPDATE job_items SET reservation_bytes=?,progress_path=? WHERE item_id=?", (size, progress, item["item_id"]))
                request.update({"operation": "hash", "expected_fingerprint": live["fingerprint"], "read_allowance": size,
                                "max_file_bytes": limits["max_file_bytes"], "chunk_bytes": opts["chunk_bytes"], "progress_path": progress})
                response = worker_fn(request, min(limits["file_timeout_seconds"], remaining))
                if not response["ok"]:
                    current = db.execute("SELECT * FROM job_items WHERE item_id=?", (item["item_id"],)).fetchone()
                    observed = recover_attempt(db, config, current)
                    with db: finish_error(db, item, response["error"], limits["max_attempts"], observed, live["fs"])
                    continue
                result = response["result"]
                read = result["read_bytes"]
                if not 0 <= read <= size: raise ValueError("Worker byte accounting exceeds reservation")
                with db:
                    db.execute("UPDATE jobs SET read_bytes=read_bytes+?,read_budget_used=read_budget_used-?+? WHERE job_id=?", (read, size, read, jid))
                    db.execute("UPDATE job_items SET read_bytes=read_bytes+?,reservation_bytes=0,progress_path=NULL WHERE item_id=?", (read, item["item_id"]))
                    if not result["completed"]:
                        finish_error(db, item, result["error"], limits["max_attempts"], read, result.get("before"))
                    else:
                        cur = db.execute("""INSERT INTO hash_observations(file_id,job_id,algorithm,implementation,status,digest,size,
                                          fingerprint,before_json,after_json,read_bytes,completed_at) VALUES (?,?,'sha256',?,'valid',?,?,?,?,?,?,?)""",
                                         (item["file_id"], jid, IMPLEMENTATION, result["digest"], size, result["fingerprint"],
                                          encode(result["before"]), encode(result["after"]), read, stamp()))
                        db.execute("UPDATE hash_current SET hash_id=?,status='valid',checked_at=? WHERE file_id=?", (cur.lastrowid, stamp(), item["file_id"]))
                        db.execute("UPDATE job_items SET status='done',last_error_json=NULL WHERE item_id=?", (item["item_id"],))
                Path(progress).unlink(missing_ok=True)
                log(config, {"job_id": jid, "file_id": item["file_id"], "status": "hash_complete" if result["completed"] else "hash_failed", "read_bytes": read})
        except KeyboardInterrupt:
            final = "cancelled"
        finally:
            for old in db.execute("SELECT * FROM job_items WHERE job_id=? AND progress_path IS NOT NULL", (jid,)).fetchall():
                recover_attempt(db, config, old, record_error=True)
            with db:
                db.execute("UPDATE job_items SET status='pending' WHERE job_id=? AND status='running'", (jid,))
                db.execute("UPDATE jobs SET status=?,updated_at=?,elapsed_seconds=elapsed_seconds+? WHERE job_id=?", (final, stamp(), time.monotonic() - started, jid))
            result = hash_status(db, jid)
            db.close()
        return result


def report(config, jid=None):
    db = readonly(config)
    try:
        db.execute("BEGIN")
        scope = "" if jid is None else " WHERE file_id IN (SELECT file_id FROM job_items WHERE job_id=?)"
        params = () if jid is None else (jid,)
        job = hash_status(db, jid) if jid else None
        groups = []
        for g in db.execute("SELECT algorithm,digest,size,count(*) AS members FROM active_exact_hashes" + scope + " GROUP BY algorithm,digest,size HAVING count(*)>1", params):
            members = []
            for m in db.execute("SELECT * FROM active_exact_hashes WHERE algorithm=? AND digest=? AND size=?" + (" AND file_id IN (SELECT file_id FROM job_items WHERE job_id=?)" if jid else ""),
                                (g["algorithm"], g["digest"], g["size"]) + params):
                labels = {r["tag"]: json.loads(r["reasons_json"]) for r in db.execute("SELECT * FROM labels WHERE file_id=?", (m["file_id"],))}
                members.append({"file_id": m["file_id"], "source_id": m["source_id"], "relative_path": m["relative_path"],
                                "hash_id": m["hash_id"], "hash_completed_at": m["completed_at"], "last_stat_check_at": m["checked_at"],
                                "metadata": json.loads(m["metadata_json"]) if m["metadata_json"] else None,
                                "metadata_matches_hash_state": m["metadata_fingerprint"] == m["fingerprint"], "labels": labels})
            groups.append({"algorithm": g["algorithm"], "sha256": g["digest"], "file_size": g["size"],
                           "member_count": len(members), "members": members,
                           "potential_logical_bytes_reclaimable": (len(members) - 1) * g["size"]})
        return {"job": job, "duplicate_group_count": len(groups), "duplicate_member_count": sum(g["member_count"] for g in groups),
                "redundant_file_count": sum(g["member_count"] - 1 for g in groups),
                "potential_logical_bytes_reclaimable": sum(g["potential_logical_bytes_reclaimable"] for g in groups),
                "actual_disk_space_saved": None, "groups": groups,
                "evidence_note": "complete SHA-256 match is extremely strong binary evidence, not a mathematical collision impossibility; no deletion performed",
                "freshness_note": "valid as of recorded hash/stat observations; no filesystem monitoring or report-time content reads"}
    finally: db.close()


def export_report(config, jid=None):
    from .cli import atomic_text
    import csv
    result = report(config, jid)
    base = config.outputs["reports"] / ("duplicates-" + (jid or "current"))
    atomic_text(base.with_suffix(".json"), lambda f: f.write(encode(result) + "\n"))
    def lines(f):
        for group in result["groups"]: f.write(encode(group) + "\n")
    atomic_text(base.with_suffix(".jsonl"), lines)
    def table(f):
        writer = csv.writer(f)
        writer.writerow(["algorithm", "sha256", "file_size", "member_count", "file_id", "source_id", "relative_path"])
        for group in result["groups"]:
            for member in group["members"]:
                safe = lambda s: "'" + s if s.startswith(("=", "+", "-", "@")) else s
                writer.writerow([group["algorithm"], group["sha256"], group["file_size"], group["member_count"], member["file_id"], safe(member["source_id"]), safe(member["relative_path"])])
    atomic_text(base.with_suffix(".csv"), table)
    return {"duplicate_group_count": result["duplicate_group_count"], "outputs": [str(base.with_suffix(s)) for s in [".json", ".jsonl", ".csv"]]}
