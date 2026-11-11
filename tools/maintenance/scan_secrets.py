#!/usr/bin/env python3
"""Fail when tracked files contain high-confidence credentials or private network IDs.

Run from the repository root or CI: ``python tools/maintenance/scan_secrets.py``.
Only tracked files are inspected; ignored evidence and media directories never enter
the scan. Binary files are skipped.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# These files intentionally contain the patterns below.
ALLOWLIST = {
    "tools/maintenance/scan_secrets.py",
    "tests/test_repository_policy.py",
}

PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("generic-secret-assignment", re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*['\"][^'\"]{12,}['\"]")),
    # Local macOS home paths can reveal usernames and workspace layout.
    ("local-home-path", re.compile(r"(?<![\w.])/Users/[A-Za-z0-9._-]+/")),
    # Tailscale uses 100.64.0.0/10; these addresses identify a private tailnet.
    ("tailscale-cgnat", re.compile(r"\b100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d{1,3}\.\d{1,3}\b")),
)


def tracked_files() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"unable to list tracked files: {error}") from error
    return [ROOT / name for name in result.stdout.decode("utf-8").split("\0") if name]


def scan(path: Path) -> list[tuple[int, str, str]]:
    relative = path.relative_to(ROOT).as_posix()
    if relative in ALLOWLIST:
        return []
    try:
        data = path.read_bytes()
    except OSError as error:
        return [(0, "unreadable", str(error))]
    if b"\0" in data[:8192]:
        return []
    text = data.decode("utf-8", errors="replace")
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in PATTERNS:
            if pattern.search(line):
                findings.append((number, kind, line.strip()[:160]))
    return findings


def main() -> int:
    files = tracked_files()
    if not files:
        print("scan_secrets: no tracked files (stage files first)", file=sys.stderr)
        return 0
    failures = 0
    for path in files:
        for number, kind, preview in scan(path):
            failures += 1
            print(f"{path.relative_to(ROOT)}:{number}: {kind}: {preview}")
    if failures:
        print(f"scan_secrets: {failures} finding(s) in {len(files)} tracked file(s)", file=sys.stderr)
        return 1
    print(f"scan_secrets: clean ({len(files)} tracked file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
