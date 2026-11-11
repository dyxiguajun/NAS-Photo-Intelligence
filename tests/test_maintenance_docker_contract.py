"""Docker isolation contract for the NAS evidence worker (no Docker daemon required)."""

from pathlib import Path
import re
import unittest

PROJECT = Path(__file__).resolve().parents[1]
DOCKERFILE = PROJECT / "tools" / "nas-evidence" / "Dockerfile"
RUNNER = PROJECT / "tools" / "nas-evidence" / "run-container.sh"


class DockerContractTest(unittest.TestCase):
    def test_base_image_and_packages_are_pinned(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        from_lines = [line for line in text.splitlines() if line.startswith("FROM ")]
        self.assertEqual(len(from_lines), 1)
        self.assertRegex(from_lines[0], r"^FROM [^@\s]+@sha256:[0-9a-f]{64}$")
        self.assertIn("python3=", text)
        self.assertIn("exiftool=", text)
        self.assertNotRegex(text, r"(?m)^\s*(?:ADD|COPY)\s+https?://")

    def test_runtime_isolation_flags_are_present(self):
        text = RUNNER.read_text(encoding="utf-8")
        required = (
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges:true",
            "--pids-limit 32",
            "--cpus 0.65",
            "--memory 512m",
            "--memory-swap 512m",
            "--pull never",
        )
        for flag in required:
            self.assertIn(flag, text, f"missing isolation flag: {flag}")
        self.assertTrue('--user "$account"' in text or "--user 1001:1001" in text, "container must run as an unprivileged numeric account")
        self.assertIn('case "$account" in', text)
        self.assertIn("''|0:*", text, "root uid must be rejected")
        self.assertIn('mountpoint -q "$state_volume"', text)
        self.assertIn('mountpoint -q "${NAS_EVIDENCE_MEDIA_VOLUME:', text)
        self.assertNotIn("--privileged", text)
        self.assertNotRegex(text, r"docker run[^\n]*\s--network\s+(?:host|bridge)")

    def test_media_mount_is_read_only(self):
        text = RUNNER.read_text(encoding="utf-8")
        mount = re.search(r"--mount \"type=bind,src=\$source_root/\$logical,dst=/media/\$logical([^\"]*)\"", text)
        self.assertIsNotNone(mount)
        self.assertIn("readonly", mount.group(1))


if __name__ == "__main__":
    unittest.main()
