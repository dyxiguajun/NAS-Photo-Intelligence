"""Repository boundary contract: production data, media and credentials stay out of Git."""

from pathlib import Path
import subprocess
import unittest

PROJECT = Path(__file__).resolve().parents[1]

FORBIDDEN_PREFIXES = (
    "data/",
    "reports/",
    "logs/",
    "cache/",
    "private/",
    ".venv/",
    "tools/exiftool/",
    "docs/phases/evidence/",
)
FORBIDDEN_SUFFIXES = (
    ".sqlite3",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".db",
    ".csv",
    ".jsonl",
    ".log",
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".mov",
    ".mp4",
    ".dng",
    ".cr2",
    ".nef",
    ".arw",
    ".tif",
    ".tiff",
    ".webp",
)
REQUIRED_IGNORES = (
    "data/",
    "reports/",
    "private/",
    "docs/phases/evidence/",
    "tools/exiftool/",
    ".venv/",
    "*.sqlite3",
)
MAX_TRACKED_BYTES = 2 * 1024 * 1024


def tracked_files():
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT), "ls-files", "-z"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


class RepositoryPolicyTest(unittest.TestCase):
    def test_no_production_data_media_or_evidence_is_tracked(self):
        files = tracked_files()
        if not files:
            self.skipTest("not a Git checkout with staged or committed files")
        violations = [name for name in files if name.startswith(FORBIDDEN_PREFIXES) or name.endswith(FORBIDDEN_SUFFIXES)]
        self.assertEqual(violations, [], f"forbidden tracked paths: {violations}")

    def test_gitignore_covers_sensitive_boundaries(self):
        text = (PROJECT / ".gitignore").read_text(encoding="utf-8")
        for entry in REQUIRED_IGNORES:
            self.assertIn(entry, text, f"missing .gitignore entry: {entry}")

    def test_tracked_text_files_stay_small(self):
        files = tracked_files()
        if not files:
            self.skipTest("not a Git checkout with staged or committed files")
        oversized = [name for name in files if (PROJECT / name).is_file() and (PROJECT / name).stat().st_size > MAX_TRACKED_BYTES]
        self.assertEqual(oversized, [], f"tracked files exceed {MAX_TRACKED_BYTES} bytes: {oversized}")


if __name__ == "__main__":
    unittest.main()
