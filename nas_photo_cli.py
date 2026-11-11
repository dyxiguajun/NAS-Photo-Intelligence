#!/usr/bin/env python3
"""Run directly without installing any Python packages."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from nas_photo.cli import main
if __name__ == "__main__": raise SystemExit(main())
