from contextlib import closing, contextmanager
import base64
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from nas_photo.config import Config, relative
from nas_photo.engine import create_job, run_job, recalculate
from nas_photo.process import run_worker
from nas_photo.store import connect
from nas_photo.time_rules import parse_time, analyze
from nas_photo.metadata import signature_format, secure_path
from nas_photo.cli import export

RULES = {"early_filesystem_before": "2000-01-01", "future_tolerance_days": 30, "conflict_tolerance_hours": 12}
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


class TimeTests(unittest.TestCase):
    def test_worker_refuses_shared_process_group(self):
        env = {**os.environ, "PYTHONPATH": str(PROJECT / "src")}
        p = subprocess.run([sys.executable, "-m", "nas_photo.worker"], input="{}", capture_output=True,
                           text=True, env=env, timeout=3)
        self.assertEqual(p.returncode, 2)
        self.assertIn("isolated supervisor", p.stderr)

    def test_overflow_and_non_finite_preserve_raw(self):
        for raw in ["1.8446744073709523e19", "NaN", "Infinity", "1e1000"]:
            with self.subTest(raw=raw):
                t = parse_time("birth", raw, "filesystem", epoch=True)
                self.assertEqual(t["raw"], raw); self.assertIsNone(t["parsed"]); self.assertTrue(t["error"])

    def test_naive_is_not_utc(self):
        t = parse_time("dto", "2023:01:02 03:04:05", "exif_original")
        self.assertEqual(t["parsed"], "2023-01-02T03:04:05")
        self.assertIsNone(t["utc"]); self.assertEqual(t["timezone"], "unknown")

    def test_explicit_offset(self):
        t = parse_time("dto", "2022:06:21 07:29:45", "exif_original", "+08:00")
        self.assertEqual(t["utc"], "2022-06-20T23:29:45+00:00")

    def test_invalid_dates_and_offsets(self):
        for raw, offset in [("0000:00:00 00:00:00", None), ("2023:02:30 12:00:00", None),
                            ("2024:01:01 12:00:00", "+08:99")]:
            self.assertTrue(parse_time("dto", raw, "exif_original", offset)["error"])

    def test_real_1998_not_declared_invalid(self):
        fs = {"mtime": "883656000", "birthtime": "883656000"}
        meta = {"kind": "image", "model": "historical camera", "time_fields": [
            {"field": "dto", "raw": "1998:01:01 12:00:00", "origin": "exif_original", "offset": "+00:00"}]}
        result = analyze(fs, meta, [], RULES, "1998/photo.jpg", NOW)
        self.assertIn("early_filesystem_timestamp", result["labels"])
        self.assertNotIn("invalid_timestamp", result["labels"])
        self.assertNotIn("missing_capture_time", result["labels"])

    def test_threshold_is_configurable(self):
        result = analyze({"mtime": "883656000"}, {"time_fields": []}, [], {**RULES, "early_filesystem_before": "1980-01-01"}, "a.jpg", NOW)
        self.assertNotIn("early_filesystem_timestamp", result["labels"])

    def test_missing_and_future(self):
        result = analyze({}, {"kind": "image", "time_fields": []}, [], RULES, "photo.jpg", NOW)
        self.assertIn("missing_capture_time", result["labels"])
        self.assertIn("metadata_incomplete", result["labels"])
        meta = {"time_fields": [{"field": "dto", "raw": "2099:01:01 12:00:00", "origin": "exif_original"}]}
        result = analyze({}, meta, [], RULES, "a.jpg", NOW)
        self.assertIn("future_timestamp", result["labels"])
        self.assertIn("timezone_uncertain", result["labels"])

    def test_copy_time_difference_is_not_a_repair_decision(self):
        meta = {"time_fields": [{"field": "dto", "raw": "2021:08:29 00:11:47", "origin": "exif_original", "offset": "+08:00"}]}
        result = analyze({"mtime": "1710599198"}, meta, [], RULES, "a.jpg", NOW)
        self.assertTrue(result["labels"]["timestamp_conflict"][0]["may_reflect_copy_or_export"])
        self.assertEqual(result["dimensions"]["time_reliability"]["judgement"], "unassessed")

    def test_unknown_timezone_does_not_create_numeric_conflict(self):
        meta = {"time_fields": [{"field": "dto", "raw": "2021:08:29 00:11:47", "origin": "exif_original"}]}
        result = analyze({"mtime": "1710599198"}, meta, [], RULES, "a.jpg", NOW)
        self.assertNotIn("timestamp_conflict", result["labels"])

    def test_format_conflict_keeps_evidence(self):
        votes = [{"tool": "signature", "format": "JPEG"}, {"tool": "old_ImageIO", "format": "HEIC", "historical": True}]
        result = analyze({}, {"time_fields": []}, votes, RULES, "a.jpg", NOW)
        self.assertEqual(result["labels"]["format_identification_conflict"][0]["observations"], votes)


FAKE = r'''#!/usr/bin/env python3
import os,sys,json,time
from pathlib import Path
base=Path(__file__).parent
if '-ver' in sys.argv:
 print('13.test');sys.exit()
path=Path(sys.argv[-1])
with (base/'calls.jsonl').open('a') as f:f.write(json.dumps({'path':str(path),'pid':os.getpid()})+'\n')
if path.name=='slow.jpg' and (base/'sleep.enabled').exists():time.sleep(30)
if path.name=='bad.jpg':
 print(json.dumps([{'ExifTool:Error':'injected parse failure'}]));sys.exit(1)
print(json.dumps([{'ExifTool:ExifToolVersion':'13.test','File:FileType':'JPEG','File:ImageWidth':2,'File:ImageHeight':2,
 'ExifIFD:DateTimeOriginal':'2023:01:02 03:04:05','ExifIFD:OffsetTimeOriginal':'+08:00',
 'IFD0:Model':'camera\u0000','ExifIFD:LensModel':'lens'}]))
'''


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "config").mkdir()
        self.mount = self.base / "mount"
        self.photos = self.mount / "photos"
        self.photos.mkdir(parents=True)
        self.fake = self.base / "fake_exiftool.py"
        self.fake.write_text(FAKE); self.fake.chmod(0o755)
        self.raw = {"devices": {"nas": {"mount_root": str(self.mount)}},
                    "sources": [{"source_id": "stable", "device_id": "nas", "logical_root": "photos"}],
                    "tools": {"exiftool": str(self.fake), "ffprobe": "ffprobe"},
                    "rules": RULES, "limits": {"file_timeout_seconds": 5, "max_attempts": 2, "run_budget_seconds": 30}}
        self.config_path = self.base / "config/local.json"
        self.config = self.reload()
        self.add("a.jpg"); self.add("b.jpg")

    def tearDown(self): self.tmp.cleanup()

    def reload(self):
        self.config_path.write_text(json.dumps(self.raw))
        return Config(self.config_path)

    def add(self, name):
        (self.photos / name).write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 32)

    def items(self, *names):
        return [{"source_id": "stable", "relative_path": n} for n in names]

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.config.outputs["database"])
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def test_incremental_skips_metadata_and_records(self):
        first = run_job(self.config, create_job(self.config, self.items("a.jpg", "b.jpg")))
        second = run_job(self.config, create_job(self.config, self.items("a.jpg", "b.jpg")))
        self.assertEqual(first["items"], {"done": 2})
        self.assertEqual(second["items"], {"skipped": 2})
        self.assertEqual(second["file_records"], 2)
        self.assertEqual(second["observation_records"], 2)
        self.assertEqual(len((self.base / "calls.jsonl").read_text().splitlines()), 2)

    def test_changed_file_only_is_reextracted(self):
        run_job(self.config, create_job(self.config, self.items("a.jpg", "b.jpg")))
        with (self.photos / "b.jpg").open("ab") as f: f.write(b"changed")
        result = run_job(self.config, create_job(self.config, self.items("a.jpg", "b.jpg")))
        self.assertEqual(result["items"], {"done": 1, "skipped": 1})
        self.assertEqual(result["observation_records"], 3)

    def test_mount_remap_preserves_identity(self):
        run_job(self.config, create_job(self.config, self.items("a.jpg")))
        other = self.base / "other-mount"
        self.mount.rename(other)
        self.raw["devices"]["nas"]["mount_root"] = str(other)
        self.config = self.reload()
        result = run_job(self.config, create_job(self.config, self.items("a.jpg")))
        self.assertEqual(result["file_records"], 1)
        self.assertEqual(result["items"], {"skipped": 1})

    def test_mount_loss_retry_restores_without_duplicate(self):
        run_job(self.config, create_job(self.config, self.items("a.jpg")))
        offline = self.base / "offline"
        self.mount.rename(offline)
        jid = create_job(self.config, self.items("a.jpg"))
        result = run_job(self.config, jid)
        self.assertEqual(result["items"], {"failed": 1})
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM errors").fetchone()[0], 2)
            self.assertIsNotNone(db.execute("SELECT metadata_json FROM files").fetchone()[0])
        offline.rename(self.mount)
        result = run_job(self.config, jid, retry_failed=True)
        self.assertEqual(result["items"], {"skipped": 1})
        self.assertEqual(result["file_records"], 1)

    def test_one_bad_file_does_not_stop_other_items(self):
        self.add("bad.jpg")
        result = run_job(self.config, create_job(self.config, self.items("bad.jpg", "a.jpg")))
        self.assertEqual(result["items"], {"done": 1, "failed": 1})

    def test_worker_timeout_is_bounded(self):
        self.add("slow.jpg"); (self.base / "sleep.enabled").touch()
        self.raw["limits"]["file_timeout_seconds"] = 0.5
        self.config = self.reload()
        t = time.monotonic()
        result = run_job(self.config, create_job(self.config, self.items("slow.jpg")))
        self.assertEqual(result["items"], {"failed": 1})
        self.assertLess(time.monotonic() - t, 5)

    def test_path_escape_and_symlink_rejected(self):
        for value in ["../private.jpg", "/etc/passwd", "a/../../x", "x\\y"]:
            with self.assertRaises(ValueError): relative(value)
        (self.photos / "link.jpg").symlink_to(self.photos / "a.jpg")
        with self.assertRaises(ValueError): secure_path(self.photos, "link.jpg")
        result = run_job(self.config, create_job(self.config, self.items("link.jpg")))
        self.assertEqual(result["items"], {"failed": 1})

    def test_local_output_cannot_overlap_input(self):
        self.raw["paths"] = {"database": "mount/photos/index.sqlite3"}
        with self.assertRaises(ValueError): self.reload()

    def test_manifest_file_cap(self):
        path = self.base / "manifest.json"
        path.write_text(json.dumps(self.items(*[f"{n}.jpg" for n in range(201)])))
        with self.assertRaises(ValueError): self.config.manifest(path)

    def test_duplicate_sources_rejected(self):
        self.raw["sources"].append({"source_id": "other", "device_id": "nas", "logical_root": "photos"})
        with self.assertRaises(ValueError): self.reload()

    def test_network_output_inside_local_project_rejected(self):
        line = f"//AI@nas/share on {self.base.resolve()}/remote (smbfs, nodev)\n"
        self.raw["paths"] = {"database": "remote/index.sqlite3"}
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=line)):
            with self.assertRaises(ValueError): self.reload()

    def test_mount_timeout_retries_and_still_rejects_network_output(self):
        line = f"//AI@nas/share on {self.base.resolve()}/remote (smbfs, nodev)\n"
        self.raw["paths"] = {"database": "remote/index.sqlite3"}
        with patch("subprocess.run", side_effect=[
                subprocess.TimeoutExpired(["mount"], 5),
                subprocess.CompletedProcess([], 0, stdout=line)]) as mount:
            with self.assertRaisesRegex(ValueError, "Output path cannot reside on a network mount"):
                self.reload()
            self.assertEqual(mount.call_count, 2)

    def test_unknown_resume_is_clear_error(self):
        with self.assertRaisesRegex(ValueError, "Unknown job_id"):
            run_job(self.config, "does-not-exist")

    def test_exhausted_crash_retries_require_explicit_retry(self):
        jid = create_job(self.config, self.items("a.jpg"))
        with self.db() as db:
            db.execute("UPDATE job_items SET attempts=2,total_attempts=2,status='running'")
        result = run_job(self.config, jid)
        self.assertEqual(result["items"], {"failed": 1})
        result = run_job(self.config, jid, retry_failed=True)
        self.assertEqual(result["items"], {"done": 1})

    def test_source_id_cannot_change_logical_identity(self):
        run_job(self.config, create_job(self.config, self.items("a.jpg")))
        self.raw["sources"][0]["logical_root"] = "other-photos"
        c = self.reload()
        with self.assertRaises(ValueError): connect(c)

    def test_discovery_caps_and_hidden_exclusion(self):
        for i in range(10): self.add(f"extra{i}.jpg")
        (self.photos / ".hidden.jpg").write_bytes(b"secret")
        result = run_worker({"operation": "discover", "root": str(self.photos), "source_id": "stable", "directory": "",
                             "max_entries": 3, "max_dirs": 1, "max_depth": 0, "max_files": 2}, 3)
        self.assertTrue(result["ok"])
        self.assertLessEqual(result["result"]["entries_examined"], 3)
        self.assertLessEqual(len(result["result"]["files"]), 2)
        self.assertTrue(result["result"]["truncated"])

    def test_recalculation_does_not_read_photos(self):
        run_job(self.config, create_job(self.config, self.items("a.jpg")))
        count = len((self.base / "calls.jsonl").read_text().splitlines())
        self.mount.rename(self.base / "offline")
        recalculate(self.config)
        self.assertEqual(len((self.base / "calls.jsonl").read_text().splitlines()), count)

    def test_reports_are_readable_and_contain_counts(self):
        jid = create_job(self.config, self.items("a.jpg"))
        run_job(self.config, jid)
        result = export(self.config, jid)
        self.assertEqual(result["summary"]["items"], {"done": 1})
        self.assertEqual(json.loads(Path(result["outputs"][1]).read_text())["relative_path"], "a.jpg")
        self.assertTrue(Path(result["outputs"][2]).read_text().startswith("file_id,"))

    def test_hard_kill_and_resume_keeps_committed_item(self):
        self.add("slow.jpg"); (self.base / "sleep.enabled").touch()
        manifest = self.base / "manifest.json"
        manifest.write_text(json.dumps(self.items("a.jpg", "slow.jpg")))
        p = subprocess.Popen([sys.executable, str(PROJECT / "nas_photo_cli.py"), "--config", str(self.config_path),
                              "scan", "--manifest", str(manifest)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            jid = json.loads(p.stdout.readline())["job_id"]
            deadline = time.monotonic() + 8
            ready = False
            while time.monotonic() < deadline:
                with self.db() as db:
                    states = dict(db.execute("SELECT status,count(*) FROM job_items GROUP BY status").fetchall())
                if states.get("done") == 1 and states.get("running") == 1:
                    ready = True; break
                time.sleep(0.05)
            self.assertTrue(ready)
            p.kill(); p.wait(timeout=2)
            (self.base / "sleep.enabled").unlink()
            result = run_job(self.config, jid)
            self.assertEqual(result["items"], {"done": 2})
            self.assertEqual(result["observation_records"], 2)
        finally:
            if p.poll() is None: p.kill(); p.wait(timeout=2)
            p.stdout.close(); p.stderr.close()

    def test_safe_cancellation_keeps_queue_and_can_resume(self):
        jid = create_job(self.config, self.items("a.jpg", "b.jpg"))
        from nas_photo.process import run_worker as actual_worker
        calls = 0
        def interrupt_second(request, timeout):
            nonlocal calls
            calls += 1
            if calls == 2: raise KeyboardInterrupt
            return actual_worker(request, timeout)
        with patch("nas_photo.engine.run_worker", side_effect=interrupt_second):
            result = run_job(self.config, jid)
        self.assertEqual(result["job"]["status"], "cancelled")
        self.assertEqual(result["items"], {"done": 1, "pending": 1})
        result = run_job(self.config, jid)
        self.assertEqual(result["items"], {"done": 2})
        self.assertEqual(result["observation_records"], 2)

    def test_real_exiftool_png_metadata_only(self):
        binary = PROJECT / "tools/exiftool/exiftool"
        if not binary.exists(): self.skipTest("install pinned ExifTool first")
        png = self.photos / "tiny.png"
        png.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="))
        self.raw["tools"]["exiftool"] = str(binary)
        self.config = self.reload()
        before = png.read_bytes()
        result = run_job(self.config, create_job(self.config, self.items("tiny.png")))
        self.assertEqual(result["items"], {"done": 1})
        with self.db() as db:
            meta = json.loads(db.execute("SELECT metadata_json FROM files").fetchone()[0])
        self.assertEqual(meta["metadata"]["format"], "PNG")
        self.assertEqual(meta["metadata"]["width"], 1)
        self.assertEqual(png.read_bytes(), before)


if __name__ == "__main__": unittest.main()
