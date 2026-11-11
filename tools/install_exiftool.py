#!/usr/bin/env python3
"""Fetch an exact upstream tool revision; never update an existing checkout."""
from pathlib import Path
import subprocess

COMMIT = "2200871d9cef988051d2a99d67df3bda6cbb30a8"
TAG = "13.59"
REPOSITORY = "https://github.com/exiftool/exiftool.git"


def main():
    target = Path(__file__).resolve().parent / "exiftool"
    if not target.exists():
        subprocess.run(["git", "clone", "--depth", "1", "--branch", TAG, REPOSITORY, str(target)], check=True, timeout=120)
    actual = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True, timeout=10).strip()
    if actual != COMMIT:
        raise SystemExit("Existing ExifTool checkout differs from the pinned revision; nothing was changed.")
    version = subprocess.check_output([str(target / "exiftool"), "-config", "", "-ver"], text=True, timeout=10).strip()
    print(f"ExifTool {version}; verified commit {actual}")


if __name__ == "__main__": main()
