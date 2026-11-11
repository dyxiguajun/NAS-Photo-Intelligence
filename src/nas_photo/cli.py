import argparse
import csv
import json
import os
import re
import signal
import sqlite3
import sys
import uuid
from pathlib import Path
from .config import Config, relative
from .engine import create_job, run_job, summary, recalculate
from .process import run_worker
from .store import encode


def read_db(config):
    path = config.outputs["database"]
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def atomic_text(path, writer):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", newline="") as f:
            writer(f)
            f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def export(config, job_id):
    db = read_db(config)
    try:
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone():
            raise ValueError("Unknown job_id")
        result = summary(db, job_id)
        result["database_bytes"] = config.outputs["database"].stat().st_size
        wal = Path(str(config.outputs["database"]) + "-wal")
        result["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
        base = config.outputs["reports"] / job_id
        atomic_text(base.with_suffix(".json"), lambda f: f.write(encode(result) + "\n"))
        query = """SELECT f.*,i.status AS job_item_status,i.total_attempts FROM files f
                   JOIN job_items i ON i.file_id=f.file_id WHERE i.job_id=? ORDER BY i.item_id"""
        def rows():
            for row in db.execute(query, (job_id,)):
                r = dict(row)
                for key in ["fs_json", "metadata_json", "dimensions_json", "last_error_json"]:
                    value = r.pop(key)
                    r[key.removesuffix("_json")] = json.loads(value) if value else None
                r["labels"] = {x["tag"]: json.loads(x["reasons_json"]) for x in db.execute("SELECT * FROM labels WHERE file_id=?", (row["file_id"],))}
                yield r
        def jsonl(f):
            for r in rows(): f.write(encode(r) + "\n")
        atomic_text(base.with_suffix(".jsonl"), jsonl)
        def csv_rows(f):
            w = csv.writer(f)
            w.writerow(["file_id", "source_id", "relative_path", "status", "format", "labels"])
            for r in rows():
                def safe(v):
                    s = str(v)
                    return "'" + s if s.startswith(("=", "+", "-", "@")) else s
                w.writerow([r["file_id"], r["source_id"], safe(r["relative_path"]), r["job_item_status"],
                            (r["metadata"] or {}).get("metadata", {}).get("format"), ";".join(r["labels"])])
        atomic_text(base.with_suffix(".csv"), csv_rows)
        return {"summary": result, "outputs": [str(base.with_suffix(s)) for s in [".json", ".jsonl", ".csv"]]}
    finally: db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded read-only NAS photo evidence index and exact duplicates")
    parser.add_argument("--config", default="config/local.json")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan"); scan.add_argument("--manifest", required=True)
    resume = sub.add_parser("resume"); resume.add_argument("job_id"); resume.add_argument("--retry-failed", action="store_true")
    sub.add_parser("status").add_argument("--job")
    sub.add_parser("export").add_argument("job_id")
    sub.add_parser("recalculate")
    d = sub.add_parser("discover")
    d.add_argument("--source", required=True); d.add_argument("--directory", default="")
    d.add_argument("--max-files", type=int, default=25); d.add_argument("--max-entries", type=int, default=200)
    d.add_argument("--max-dirs", type=int, default=10); d.add_argument("--max-depth", type=int, default=2)
    dup = sub.add_parser("duplicates", help="Exact binary duplicates; never deletes files")
    ds = dup.add_subparsers(dest="duplicate_command", required=True)
    ds.add_parser("migrate", help="Upgrade compatible schema after local recovery backups")
    ds.add_parser("candidates", help="Size groups only; no source I/O").add_argument("--manifest")
    h = ds.add_parser("hash", help="Hash only candidates inside an explicit manifest")
    h.add_argument("--manifest", required=True); h.add_argument("--force-rehash", action="store_true")
    h.add_argument("--include-non-candidates", action="store_true")
    h.add_argument("--max-read-bytes", type=int); h.add_argument("--chunk-bytes", type=int)
    hr = ds.add_parser("resume"); hr.add_argument("job_id"); hr.add_argument("--retry-failed", action="store_true")
    ds.add_parser("status").add_argument("--job")
    hp = ds.add_parser("report"); hp.add_argument("--job"); hp.add_argument("--export", action="store_true")
    sim = sub.add_parser("similarity", help="Bounded pixel equality and visual candidates; no deletion")
    ss = sim.add_subparsers(dest="similarity_command", required=True)
    sa = ss.add_parser("analyze"); sa.add_argument("--manifest", required=True)
    sa.add_argument("--force", action="store_true"); sa.add_argument("--max-read-bytes", type=int)
    sr = ss.add_parser("resume"); sr.add_argument("job_id"); sr.add_argument("--retry-failed", action="store_true")
    ss.add_parser("status").add_argument("--job")
    for name in ("compare", "candidates"):
        sc = ss.add_parser(name); sc.add_argument("--job", required=True)
        sc.add_argument("--phash-distance", type=int, default=8); sc.add_argument("--dhash-distance", type=int, default=12)
        sc.add_argument("--aspect-tolerance", type=float, default=.08); sc.add_argument("--color-difference", type=float, default=35)
    sp = ss.add_parser("report"); sp.add_argument("--job", required=True); sp.add_argument("--export", action="store_true")
    from . import organize
    organize.configure(sub.add_parser("organize", help="Independent inventory and read-only library simulation"))
    args = parser.parse_args(argv)
    def terminate(signum, frame): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        if args.command == "organize":
            print(encode(organize.run(args)), flush=True)
            return 0
        config = Config(args.config)
        if args.command == "similarity":
            from . import similarity as visual
            command = args.similarity_command
            if command in ("analyze", "resume"):
                jid = visual.create_job(config, config.manifest(args.manifest), visual.options(config, args.force, args.max_read_bytes)) if command == "analyze" else args.job_id
                print(encode({"job_id": jid}), flush=True)
                result = visual.run_job(config, jid, getattr(args, "retry_failed", False))
                print(encode(result), flush=True)
                return 0 if result["job"]["status"] == "completed" else 2
            if command == "status":
                db = read_db(config)
                try: result = visual.status(db, args.job) if args.job else {"jobs": [dict(r) for r in db.execute("SELECT job_id,status,read_bytes,read_budget_used FROM jobs WHERE kind='visual' ORDER BY created_at DESC LIMIT 20")]}
                finally: db.close()
            elif command in ("compare", "candidates"):
                result = visual.compare(config, args.job, visual.comparison_options(args.phash_distance,args.dhash_distance,args.aspect_tolerance,args.color_difference))
            else: result = visual.export_report(config,args.job) if args.export else visual.report(config,args.job)
            print(encode(result), flush=True)
            return 0
        if args.command == "duplicates":
            from . import duplicates as exact
            if args.duplicate_command in ("hash", "resume"):
                if args.duplicate_command == "hash":
                    opts = exact.options(config, args.force_rehash, args.include_non_candidates, args.max_read_bytes, args.chunk_bytes)
                    jid = exact.create_hash_job(config, config.manifest(args.manifest), opts)
                else: jid = args.job_id
                print(encode({"job_id": jid}), flush=True)
                result = exact.run_hash_job(config, jid, getattr(args, "retry_failed", False))
                print(encode(result), flush=True)
                return 0 if result["job"]["status"] == "completed" else 2
            if args.duplicate_command == "candidates":
                result = exact.candidates(config, config.manifest(args.manifest) if args.manifest else None)
            elif args.duplicate_command == "migrate":
                from .engine import exclusive
                from .store import connect
                with exclusive(config):
                    db = connect(config)
                    result = {"schema_version": db.execute("PRAGMA user_version").fetchone()[0]}
                    db.close()
            elif args.duplicate_command == "status":
                db = exact.readonly(config)
                try:
                    result = exact.hash_status(db, args.job) if args.job else {"jobs": [dict(r) for r in db.execute("SELECT job_id,status,read_bytes,read_budget_used,accounting_complete FROM jobs WHERE kind='hash' ORDER BY created_at DESC LIMIT 20")]}
                finally: db.close()
            elif args.duplicate_command == "report":
                result = exact.export_report(config, args.job) if args.export else exact.report(config, args.job)
            print(encode(result), flush=True)
            return 0
        if args.command in ["scan", "resume"]:
            jid = create_job(config, config.manifest(args.manifest)) if args.command == "scan" else args.job_id
            print(encode({"job_id": jid}), flush=True)
            result = run_job(config, jid, getattr(args, "retry_failed", False))
            print(encode(result), flush=True)
            return 0 if result["job"]["status"] == "completed" else 2
        if args.command == "status":
            db = read_db(config)
            try:
                if args.job and not db.execute("SELECT 1 FROM jobs WHERE job_id=?", (args.job,)).fetchone():
                    raise ValueError("Unknown job_id")
                result = summary(db, args.job)
                result["jobs"] = [dict(r) for r in db.execute("SELECT job_id,status,updated_at FROM jobs ORDER BY created_at DESC LIMIT 20")]
            finally: db.close()
        elif args.command == "export": result = export(config, args.job_id)
        elif args.command == "recalculate":
            recalculate(config); result = {"recalculated": True, "input_files_read": 0}
        elif args.command == "discover":
            if args.source not in config.sources: raise ValueError("Unknown source")
            if not (1 <= args.max_files <= config.limits["max_files"] and 1 <= args.max_entries <= 1000
                    and 1 <= args.max_dirs <= 50 and 0 <= args.max_depth <= 5):
                raise ValueError("Discovery limits exceed Phase 1 safety bounds")
            result = run_worker({"operation": "discover", "root": config.sources[args.source]["root"],
                                 "source_id": args.source, "directory": relative(args.directory, True),
                                 "max_files": args.max_files, "max_entries": args.max_entries,
                                 "max_dirs": args.max_dirs, "max_depth": args.max_depth}, config.limits["file_timeout_seconds"])
            if not result["ok"]: raise ValueError(str(result["error"]))
            result = result["result"]
            name = "discovery-" + uuid.uuid4().hex
            manifest = config.outputs["reports"] / (name + ".manifest.json")
            atomic_text(manifest, lambda f: f.write(encode(result["files"]) + "\n"))
            atomic_text(manifest.with_suffix(".diagnostics.json"), lambda f: f.write(encode(result) + "\n"))
            result["manifest"] = str(manifest)
        print(encode(result), flush=True)
        return 0
    except KeyboardInterrupt:
        print(encode({"status": "cancelled", "message": "committed progress retained"}), file=sys.stderr)
        return 130
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as e:
        print(encode({"error": type(e).__name__, "message": str(e)[:700]}), file=sys.stderr)
        return 1


if __name__ == "__main__": sys.exit(main())
