from contextlib import closing
import hashlib
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
import test_engine as baseline

from nas_photo.config import Config
from nas_photo import duplicates as dup
from nas_photo.hashing import hash_file
from nas_photo.metadata import snapshot, fingerprint
from nas_photo.store import connect, encode, stamp, SCHEMA
from nas_photo.process import run_worker

PROJECT = Path(__file__).resolve().parents[1]


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = baseline.EngineTests()
        self.fixture.setUp()
        self.base = self.fixture.base
        self.raw = self.fixture.raw
        self.config = self.fixture.config
        self.photos = self.fixture.photos

    def tearDown(self): self.fixture.tearDown()

    def reload(self):
        self.fixture.raw = self.raw
        self.config = self.fixture.reload()

    def seed(self, name, content, source="stable"):
        root = Path(self.config.sources[source]["root"])
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
        fs, _ = snapshot(path)
        db = connect(self.config)
        metadata = {"metadata": {"kind": "video" if name.endswith(".mov") else "image", "format": "fixture"}, "raw": {}, "format_votes": []}
        with db:
            db.execute("""INSERT INTO files(source_id,relative_path,fingerprint,fs_json,metadata_json,status,first_seen,last_seen)
                       VALUES (?,?,?,?,?,'ready',?,?)""", (source, name, fingerprint(fs), encode(fs), encode(metadata), stamp(), stamp()))
        db.close()
        return {"source_id": source, "relative_path": name}

    def run_hash(self, items, **kwargs):
        jid = dup.create_hash_job(self.config, items, dup.options(self.config, **kwargs))
        return jid, dup.run_hash_job(self.config, jid)

    def db(self): return closing(dup.readonly(self.config))

    def test_renamed_and_cross_source_duplicates(self):
        self.raw["sources"].append({"source_id": "other", "device_id": "nas", "logical_root": "other-photos"})
        self.reload()
        items = [self.seed("first.jpg", b"identical bytes"), self.seed("renamed.bin", b"identical bytes"),
                 self.seed("nested/third.png", b"identical bytes", "other")]
        jid, result = self.run_hash(items)
        report = dup.report(self.config, jid)
        self.assertEqual(result["items"], {"done": 3})
        self.assertEqual(report["duplicate_group_count"], 1)
        self.assertEqual(report["duplicate_member_count"], 3)
        self.assertEqual(report["potential_logical_bytes_reclaimable"], 2 * len(b"identical bytes"))
        self.assertIsNone(report["actual_disk_space_saved"])
        self.assertEqual(report["groups"][0]["sha256"], hashlib.sha256(b"identical bytes").hexdigest())

    def test_same_name_same_size_different_content(self):
        items = [self.seed("a/same.jpg", b"1234"), self.seed("b/same.jpg", b"4321")]
        self.assertEqual(dup.candidates(self.config, items)["candidate_files"], 2)
        jid, result = self.run_hash(items)
        self.assertEqual(result["program_read_bytes_observed"], 8)
        self.assertEqual(dup.report(self.config, jid)["duplicate_group_count"], 0)

    def test_non_candidate_does_not_read_or_claim_uniqueness(self):
        item = self.seed("only.jpg", b"only")
        jid, result = self.run_hash([item])
        self.assertEqual(result["items"], {"not_candidate": 1})
        self.assertEqual(result["program_read_bytes_observed"], 0)
        self.assertTrue(dup.candidates(self.config, [item])["non_candidates_are_not_proven_unique"])
        second = self.seed("later.jpg", b"only")
        _, result = self.run_hash([item, second])
        self.assertEqual(result["items"], {"done": 2})

    def test_cache_and_explicit_force(self):
        items = [self.seed("one.jpg", b"copy"), self.seed("two.jpg", b"copy")]
        self.run_hash(items)
        _, cached = self.run_hash(items)
        self.assertEqual(cached["items"], {"skipped": 2})
        self.assertEqual(cached["program_read_bytes_observed"], 0)
        self.assertEqual(cached["cache_saved_content_bytes"], 8)
        _, forced = self.run_hash(items, force=True)
        self.assertEqual(forced["program_read_bytes_observed"], 8)
        self.assertEqual(forced["items"], {"done": 2})

    def test_same_size_change_invalidates_and_updates_group(self):
        items = [self.seed("one.jpg", b"copy"), self.seed("two.jpg", b"copy"), self.seed("three.jpg", b"copy")]
        self.run_hash(items)
        path = self.photos / "two.jpg"
        path.write_bytes(b"else")
        old = os.stat(path).st_mtime_ns
        os.utime(path, ns=(old, old + 1000000000))
        _, result = self.run_hash(items)
        self.assertEqual(result["items"], {"done": 1, "skipped": 2})
        report = dup.report(self.config)
        self.assertEqual(report["duplicate_member_count"], 2)
        self.assertNotIn("two.jpg", [m["relative_path"] for m in report["groups"][0]["members"]])

    def test_size_change_rehash_when_explicitly_included(self):
        item = self.seed("one.jpg", b"copy")
        self.run_hash([item], include_non_candidates=True)
        (self.photos / "one.jpg").write_bytes(b"longer")
        _, result = self.run_hash([item], include_non_candidates=True)
        self.assertEqual(result["items"], {"done": 1})
        self.assertEqual(result["program_read_bytes_observed"], 6)

    def test_metadata_update_trigger_removes_stale_membership(self):
        items = [self.seed("one.jpg", b"copy"), self.seed("two.jpg", b"copy")]
        self.run_hash(items)
        db = connect(self.config)
        with db: db.execute("UPDATE files SET fingerprint='changed-index-state' WHERE relative_path='two.jpg'")
        db.close()
        self.assertEqual(dup.report(self.config)["duplicate_group_count"], 0)

    def test_budget_persists_and_does_not_overread(self):
        items = [self.seed("one.jpg", b"copy"), self.seed("two.jpg", b"copy")]
        jid, result = self.run_hash(items, max_read_bytes=4)
        self.assertEqual(result["job"]["status"], "budget_exhausted")
        self.assertEqual(result["program_read_bytes_observed"], 4)
        again = dup.run_hash_job(self.config, jid)
        self.assertEqual(again["program_read_bytes_observed"], 4)
        self.assertEqual(again["read_budget_charged_bytes"], 4)

    def test_zero_byte_budget_can_reuse_cached_hash(self):
        items = [self.seed("one.jpg", b"copy"), self.seed("two.jpg", b"copy")]
        self.run_hash(items)
        _, result = self.run_hash(items, max_read_bytes=0)
        self.assertEqual(result["items"], {"skipped": 2})
        self.assertEqual(result["program_read_bytes_observed"], 0)

    def test_preserved_size_mtime_limitation_and_force_verification(self):
        items=[self.seed('one.jpg',b'copy'),self.seed('two.jpg',b'copy')]
        self.run_hash(items)
        path=self.photos/'two.jpg';old=os.stat(path)
        path.write_bytes(b'else');os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
        _,cached=self.run_hash(items)
        self.assertEqual(cached['items'],{'skipped':2})
        self.assertEqual(dup.report(self.config)['duplicate_group_count'],1)
        self.run_hash(items,force=True)
        self.assertEqual(dup.report(self.config)['duplicate_group_count'],0)

    def test_hardlinks_remain_logical_members_not_physical_savings(self):
        a=self.seed('one.jpg',b'copy');b=self.seed('linked.jpg',b'copy')
        (self.photos/'linked.jpg').unlink();os.link(self.photos/'one.jpg',self.photos/'linked.jpg')
        jid,_=self.run_hash([a,b])
        report=dup.report(self.config,jid)
        self.assertEqual(report['duplicate_member_count'],2)
        self.assertEqual(report['potential_logical_bytes_reclaimable'],4)
        self.assertIsNone(report['actual_disk_space_saved'])

    def test_empty_and_tiny_streams(self):
        items = [self.seed("empty1.jpg", b""), self.seed("empty2.jpg", b""), self.seed("tiny1.jpg", b"x"), self.seed("tiny2.jpg", b"x")]
        jid, result = self.run_hash(items, chunk_bytes=1)
        self.assertEqual(result["program_read_bytes_observed"], 2)
        report = dup.report(self.config, jid)
        self.assertEqual(report["duplicate_group_count"], 2)
        self.assertIn(hashlib.sha256(b"").hexdigest(), [g["sha256"] for g in report["groups"]])

    def direct_request(self, item):
        fs, _ = snapshot(self.photos / item["relative_path"])
        self.config.outputs["cache"].mkdir(exist_ok=True)
        fd, progress = tempfile.mkstemp(prefix="hash-", dir=self.config.outputs["cache"])
        os.write(fd, b"0" * 24); os.close(fd)
        return {"root": str(self.photos), "relative_path": item["relative_path"], "expected_fingerprint": fingerprint(fs),
                "max_file_bytes": 1024**2, "read_allowance": fs["size"], "chunk_bytes": 2, "progress_path": progress}

    def test_modification_during_hash_never_publishes_digest(self):
        item = self.seed("during.jpg", b"abcdefgh")
        request = self.direct_request(item)
        actual = os.read
        def modifying(fd, n):
            data = actual(fd, n)
            with (self.photos / "during.jpg").open("ab") as f: f.write(b"changed")
            return data
        with patch("nas_photo.hashing.read_chunk", side_effect=modifying): result = hash_file(request)
        self.assertFalse(result["completed"])
        self.assertIsNone(result["digest"])
        self.assertIn("changed", result["error"]["message"])

    def test_partial_read_exception_does_not_return_partial_hash(self):
        item = self.seed("broken.jpg", b"abcdefgh")
        request = self.direct_request(item)
        calls = 0
        def broken(fd, n):
            nonlocal calls
            calls += 1
            if calls == 2: raise OSError("injected network interruption")
            return os.read(fd, n)
        with patch("nas_photo.hashing.read_chunk", side_effect=broken): result = hash_file(request)
        self.assertFalse(result["completed"]); self.assertIsNone(result["digest"])
        self.assertEqual(result["read_bytes"], 2)

    def test_mount_remap_reuses_identity_and_cached_hash(self):
        items = [self.seed("one.jpg", b"copy"), self.seed("two.jpg", b"copy")]
        self.run_hash(items)
        other = self.base / "alternate"
        self.fixture.mount.rename(other)
        self.raw["devices"]["nas"]["mount_root"] = str(other); self.reload()
        _, result = self.run_hash(items)
        self.assertEqual(result["items"], {"skipped": 2})
        with self.db() as db: self.assertEqual(db.execute("SELECT count(*) FROM files").fetchone()[0], 2)

    def test_unavailable_input_and_explicit_retry(self):
        items = [self.seed("one.jpg", b"copy"), self.seed("two.jpg", b"copy")]
        self.run_hash(items)
        offline = self.base / "offline"; self.fixture.mount.rename(offline)
        jid, result = self.run_hash(items)
        self.assertEqual(result["items"], {"failed": 2})
        self.assertEqual(dup.report(self.config)["duplicate_group_count"], 0)
        offline.rename(self.fixture.mount)
        result = dup.run_hash_job(self.config, jid, retry_failed=True)
        self.assertEqual(result["items"], {"skipped": 2})
        self.assertEqual(dup.report(self.config)["duplicate_group_count"], 1)

    def test_symlink_hash_is_rejected(self):
        one = self.seed("one.jpg", b"copy")
        two = self.seed("link.jpg", b"copy")
        (self.photos / "link.jpg").unlink(); (self.photos / "link.jpg").symlink_to(self.photos / "one.jpg")
        jid, result = self.run_hash([one, two])
        self.assertEqual(result["items"], {"done": 1, "failed": 1})
        self.assertEqual(dup.report(self.config, jid)["duplicate_group_count"], 0)

    def test_live_photo_candidates_are_not_duplicate_relationships(self):
        a = self.seed("IMG_1.heic", b"photo")
        b = self.seed("IMG_1.mov", b"video")
        db = connect(self.config)
        with db: db.execute("INSERT INTO candidate_relations VALUES (1,2,'live_photo_candidate','{}','unverified')")
        db.close()
        jid, _ = self.run_hash([a, b])
        self.assertEqual(dup.report(self.config, jid)["duplicate_group_count"], 0)
        with self.db() as db: self.assertEqual(db.execute("SELECT count(*) FROM candidate_relations").fetchone()[0], 1)

    def test_report_export_has_metadata_and_logical_bytes_only(self):
        items = [self.seed("one.jpg", b"copy"), self.seed("two.jpg", b"copy")]
        jid, _ = self.run_hash(items)
        out = dup.export_report(self.config, jid)
        report = json.loads(Path(out["outputs"][0]).read_text())
        self.assertIn("metadata", report["groups"][0]["members"][0])
        self.assertIsNone(report["actual_disk_space_saved"])
        self.assertTrue(Path(out["outputs"][2]).read_text().startswith("algorithm,"))

    def test_safe_cancel_does_not_commit_partial_hash(self):
        items = [self.seed("one.jpg", b"copy"), self.seed("two.jpg", b"copy")]
        jid = dup.create_hash_job(self.config, items, dup.options(self.config))
        def interrupt(req, timeout):
            if req['operation']=='hash' and req['relative_path']=='two.jpg':
                Path(req['progress_path']).write_text(f'{2:024d}')
                raise KeyboardInterrupt
            return run_worker(req, timeout)
        result = dup.run_hash_job(self.config, jid, worker_fn=interrupt)
        self.assertEqual(result['job']['status'],'cancelled')
        self.assertEqual(result['items'],{'done':1,'pending':1})
        self.assertFalse(result['read_bytes_are_exact'])
        with self.db() as db: self.assertEqual(db.execute("SELECT count(*) FROM hash_observations WHERE status='valid'").fetchone()[0],1)
        result = dup.run_hash_job(self.config,jid)
        self.assertEqual(result['items'],{'done':2})
        self.assertEqual(result['program_read_bytes_observed'],10)

    def slow_site(self):
        directory=self.base/'inject';directory.mkdir()
        (directory/'sitecustomize.py').write_text("import time\nimport nas_photo.hashing as h\noriginal=h.read_chunk\ndef slow(fd,n):\n time.sleep(0.03)\n return original(fd,n)\nh.read_chunk=slow\n")
        return str(directory)+os.pathsep+str(PROJECT/'src')

    def test_worker_timeout_during_real_stream(self):
        items=[self.seed('slow1.jpg',b'x'*65536),self.seed('slow2.jpg',b'x'*65536)]
        self.raw['limits']['file_timeout_seconds']=0.3;self.raw['limits']['max_attempts']=1;self.reload()
        with patch.dict(os.environ,{'PYTHONPATH':self.slow_site()}):
            jid,result=self.run_hash(items,chunk_bytes=1024)
        self.assertEqual(result['items'],{'failed':2})
        self.assertFalse(result['read_bytes_are_exact'])
        self.assertLessEqual(result['read_budget_charged_bytes'],2*65536)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hash_observations WHERE status='valid'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT sum(read_bytes) FROM hash_observations WHERE status='failed'").fetchone()[0],result['program_read_bytes_observed'])

    def test_hard_kill_then_resume_keeps_complete_hash_only(self):
        items=[self.seed('first.jpg',b'a'),self.seed('long.jpg',b'x'*65536)]
        manifest=self.base/'scope.json';manifest.write_text(json.dumps(items))
        env={**os.environ,'PYTHONPATH':self.slow_site()}
        p=subprocess.Popen([sys.executable,str(PROJECT/'nas_photo_cli.py'),'--config',str(self.fixture.config_path),
                            'duplicates','hash','--manifest',str(manifest),'--include-non-candidates','--chunk-bytes','1024'],
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        try:
            jid=json.loads(p.stdout.readline())['job_id']
            ready=False;deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                with self.db() as db:
                    row=db.execute("SELECT progress_path FROM job_items WHERE status='running' AND progress_path IS NOT NULL ORDER BY item_id DESC LIMIT 1").fetchone()
                    completed=db.execute("SELECT count(*) FROM job_items WHERE status='done'").fetchone()[0]
                if row and completed==1 and Path(row[0]).exists() and int(Path(row[0]).read_text())>0:
                    ready=True;break
                time.sleep(0.03)
            self.assertTrue(ready)
            p.kill();p.wait(timeout=2)
            time.sleep(0.35)
            with self.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM hash_observations WHERE status='valid'").fetchone()[0],1)
            result=dup.run_hash_job(self.config,jid)
            self.assertEqual(result['items'],{'done':2})
            self.assertFalse(result['read_bytes_are_exact'])
            with self.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM hash_observations WHERE status='valid'").fetchone()[0],2)
        finally:
            if p.poll() is None:p.kill();p.wait(timeout=2)
            p.stdout.close();p.stderr.close()

    def test_migration_preserves_v1_rows_and_is_idempotent(self):
        path=self.config.outputs['database'];path.parent.mkdir(exist_ok=True)
        db=sqlite3.connect(path);db.executescript(SCHEMA)
        db.execute("INSERT INTO sources VALUES('stable','nas','photos','old','old-mount')")
        db.execute("INSERT INTO files(source_id,relative_path,status,first_seen,last_seen) VALUES('stable','old.jpg','pending','before','before')")
        db.commit();old=db.execute('SELECT * FROM files').fetchall();db.close()
        db=connect(self.config)
        self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM files')],old)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3);db.close()
        db=connect(self.config);self.assertEqual(db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0],2);db.close()
        backup=path.with_name(path.stem+'.pre-phase2.sqlite3')
        with closing(sqlite3.connect(backup)) as b:self.assertEqual(b.execute('PRAGMA user_version').fetchone()[0],1)


if __name__=='__main__':unittest.main()
