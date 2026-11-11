"""Regression tests for inventory idempotency, accounting and path safety."""

from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from nas_photo import organize_index as index

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def observation(path, size=20, mtime=1, **evidence):
    return {"path": path, "stat": [size, mtime, 2], "evidence": evidence}


@contextmanager
def database():
    with tempfile.TemporaryDirectory() as tmp:
        with index.connect(Path(tmp) / "index.sqlite3", write=True) as db:
            index.register_sources(
                db,
                [
                    {"source_id": "one", "device_id": "test", "logical_root": "first"},
                    {"source_id": "two", "device_id": "test", "logical_root": "second"},
                ],
            )
            yield db


class InventoryIntegrityTest(unittest.TestCase):
    def test_unicode_spaces_and_reserved_characters_round_trip(self):
        paths = [
            "相册/IMG 0001 (1).JPG",
            "a b/c#d?e&f.jpg",
            "目录/子目录/é.jpg",
            "emoji/📷.jpg",
            "trailing space /file.jpg",
        ]
        with database() as db:
            index.apply_batch(db, "one", "v1", [observation(path, size=number + 1) for number, path in enumerate(paths)], coverage="complete")
            rows = index.files(db)
            self.assertEqual([row["relative_path"] for row in rows], sorted(paths))
            self.assertEqual({row["state"] for row in rows}, {"present"})
            self.assertEqual(index.status(db)["registered"], len(paths))

    def test_same_size_different_content_is_not_a_duplicate(self):
        with database() as db:
            index.apply_batch(
                db,
                "one",
                "v1",
                [
                    observation("a.jpg", size=100, sha256=DIGEST_A),
                    observation("b.jpg", size=100, sha256=DIGEST_B),
                    observation("c.jpg", size=100, sha256=DIGEST_A),
                ],
                coverage="complete",
            )
            source = index.status(db)["sources"]["one"]
            self.assertEqual(source["sha_verified"], 3)
            self.assertEqual(source["l0_duplicate_paths"], 2)
            self.assertEqual(index.status(db)["cross_source_l0_groups"], 0)

    def test_replay_and_status_reads_are_idempotent(self):
        rows = [observation("a.jpg", sha256=DIGEST_A), observation("b.jpg")]
        with database() as db:
            first = index.apply_batch(db, "one", "v1", rows, coverage="complete")
            before = index.status(db)
            replay = index.apply_batch(db, "one", "v1", rows, coverage="complete")
            self.assertTrue(replay["reused"])
            self.assertEqual(replay["batch_id"], first["batch_id"])
            self.assertEqual(index.files(db)[0]["change_kind"], "new")
            self.assertEqual(index.status(db), before)
            self.assertEqual(index.status(db), before)
            self.assertEqual(len(index.files(db)), len(rows))

    def test_unavailable_and_failed_metadata_are_accounted_separately(self):
        with database() as db:
            index.apply_batch(
                db,
                "one",
                "v1",
                [
                    observation("ok.jpg", sha256=DIGEST_A, metadata={"model": "camera"}),
                    observation("pending.jpg"),
                    observation("failed.jpg", metadata_error="decode failed"),
                    {"path": "offline.jpg", "state": "unavailable", "issue": "share offline"},
                ],
            )
            source = index.status(db)["sources"]["one"]
            self.assertEqual((source["present"], source["unavailable"], source["missing"]), (3, 1, 0))
            self.assertEqual((source["metadata_processed"], source["metadata_pending"], source["metadata_failed"]), (1, 1, 1))
            self.assertEqual(source["registered"], 4)

    def test_duplicate_paths_in_one_batch_are_rejected_without_writes(self):
        with database() as db:
            with self.assertRaisesRegex(ValueError, "Duplicate relative path"):
                index.apply_batch(db, "one", "v1", [observation("a.jpg"), observation("a.jpg")])
            self.assertEqual(index.files(db), [])
            self.assertEqual(db.execute("SELECT count(*) FROM organize_batches").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
