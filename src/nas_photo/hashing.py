"""Streaming SHA-256 with explicit byte accounting and no partial digests."""
import hashlib
import os
from .metadata import secure_path, snapshot, fingerprint

IMPLEMENTATION = "hashlib.sha256/stream-v1"


def read_chunk(fd, n):
    return os.read(fd, n)


def probe(request):
    path = secure_path(request["root"], request["relative_path"])
    fs, identity = snapshot(path)
    return {"fs": fs, "identity": list(identity), "fingerprint": fingerprint(fs)}


def hash_file(request):
    total = 0
    before = after = None
    fd = counter = None
    try:
        path = secure_path(request["root"], request["relative_path"])
        before, identity = snapshot(path)
        if fingerprint(before) != request["expected_fingerprint"]:
            raise ValueError("file_changed_since_probe")
        if before["size"] > request["max_file_bytes"] or before["size"] > request["read_allowance"]:
            raise ValueError("read_budget_or_file_size_limit")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != identity:
            raise ValueError("file_replaced_before_open")
        counter = os.open(request["progress_path"], os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
        sha = hashlib.sha256()
        while total < before["size"]:
            block = read_chunk(fd, min(request["chunk_bytes"], before["size"] - total))
            if not block: raise OSError("unexpected_eof")
            total += len(block)
            # Lower bound survives a killed supervisor; budget reservation supplies the upper bound.
            os.pwrite(counter, f"{total:024d}".encode(), 0)
            sha.update(block)
        current_fd = os.fstat(fd)
        after, final_identity = snapshot(path)
        fd_identity = (current_fd.st_dev, current_fd.st_ino, current_fd.st_size, current_fd.st_mtime_ns)
        if final_identity != identity or fd_identity != identity or fingerprint(after) != fingerprint(before) or after["ctime"] != before["ctime"]:
            raise ValueError("file_changed_during_hash")
        return {"completed": True, "digest": sha.hexdigest(), "size": before["size"],
                "fingerprint": fingerprint(before), "before": before, "after": after,
                "read_bytes": total, "accounting_complete": True, "implementation": IMPLEMENTATION}
    except Exception as e:
        return {"completed": False, "digest": None, "read_bytes": total, "accounting_complete": True,
                "before": before, "after": after, "error": {"code": type(e).__name__, "message": str(e)[:500]}}
    finally:
        if fd is not None: os.close(fd)
        if counter is not None: os.close(counter)
