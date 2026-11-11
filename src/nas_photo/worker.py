"""Disposable I/O process; the supervisor owns its entire process group."""
import json
import os
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path
from .metadata import extract, secure_path, EXTENSIONS
from .config import relative


def discover(req):
    root = Path(req["root"])
    start = relative(req.get("directory", ""), True)
    queue = deque([(start, 0)])
    files, errors = [], []
    entries = dirs = 0
    truncated = False
    while queue:
        rel, depth = queue.popleft()
        if dirs >= req["max_dirs"]:
            truncated = True; break
        try:
            path = secure_path(root, rel) if rel else root.resolve(strict=True)
            if root.is_symlink(): raise ValueError("symlink_source_root")
            dirs += 1
            with os.scandir(path) as it:
                for e in it:
                    if entries >= req["max_entries"] or len(files) >= req["max_files"]:
                        truncated = True; break
                    entries += 1
                    if e.name.startswith(".") or e.is_symlink(): continue
                    child = str(Path(rel) / e.name)
                    if e.is_dir(follow_symlinks=False) and depth < req["max_depth"]:
                        queue.append((child, depth + 1))
                    elif e.is_file(follow_symlinks=False) and Path(e.name).suffix.lower()[1:] in EXTENSIONS:
                        files.append({"source_id": req["source_id"], "relative_path": child})
            if truncated: break
        except (OSError, ValueError) as e:
            errors.append({"relative_path": rel, "error": type(e).__name__, "detail": str(e)[:300]})
    return {"files": files, "errors": errors, "entries_examined": entries,
            "directories_visited": dirs, "truncated": truncated}


def main():
    if os.getpgrp() != os.getpid():
        print("worker must be launched by its isolated supervisor", file=sys.stderr)
        raise SystemExit(2)
    req = json.load(sys.stdin)
    parent = req.get("supervisor_pid", os.getppid())
    def watch_parent():
        while True:
            time.sleep(0.25)
            if os.getppid() != parent:
                os.killpg(os.getpgrp(), signal.SIGTERM)
                return
    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        operation = req.get("operation")
        if operation == "visual":
            from .imaging import analyze_file
            result = analyze_file(req)
        elif operation in ("hash", "hash_probe"):
            from .hashing import hash_file, probe
            result = hash_file(req) if operation == "hash" else probe(req)
        else:
            result = discover(req) if operation == "discover" else extract(req)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, allow_nan=False))
    except Exception as e:
        print(json.dumps({"ok": False, "error": {"code": type(e).__name__, "message": str(e)[:700]}}, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__": main()
