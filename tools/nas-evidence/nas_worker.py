"""Run via SSH in the DataRestore1 root. Only opens regular source files for reading."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

MAX_BYTES = 1024**3
MAX_FILES = 6000
ROOT = Path.cwd()


def emit(value):
    print(json.dumps(value), flush=True)


def identity(s):
    return (s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def read_one(path, expected, fetch, allowed_branches=('Photos', 'JPEG Images', 'PNG Images')):
    relative = Path(path)
    assert relative.parts and (allowed_branches is None or relative.parts[0] in allowed_branches)
    assert not relative.is_absolute() and '..' not in relative.parts
    assert relative.resolve(strict=True) == ROOT / relative
    size = expected[0]
    sha = hashlib.sha256()
    with os.fdopen(os.open(relative, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or identity(before) != expected:
            raise ValueError('source stat changed')
        remaining = size
        while remaining:
            block = stream.read(min(remaining, 1024**2))
            if not block:
                raise ValueError('unexpected EOF')
            remaining -= len(block)
            sha.update(block)
            if fetch:
                sys.stdout.buffer.write(block)
        after = os.fstat(stream.fileno())
        final = relative.stat(follow_symlinks=False)
        if any(identity(s) != expected or s.st_ino != before.st_ino or s.st_dev != before.st_dev
               for s in (after, final)):
            raise ValueError('source changed during read')
    return sha.hexdigest()


def main():
    mode = sys.argv[1]
    rows = json.load(sys.stdin)
    assert 0 < len(rows) <= MAX_FILES and sum(row[1] for row in rows) <= MAX_BYTES
    if mode == 'fetch':
        assert len(rows) == 1 and rows[0][1] <= 64*1024**2
        read_one(rows[0][0], tuple(rows[0][1:]), True)
        return
    assert mode in ('sha256', 'sha256-source')
    allowed_branches = None if mode == 'sha256-source' else ('Photos', 'JPEG Images', 'PNG Images')
    emit({'pid': os.getpid(), 'requested': len(rows)})
    for path, *expected in rows:
        try:
            emit([path, read_one(path, tuple(expected), False, allowed_branches), None])
        except (ValueError, OSError, AssertionError) as error:
            emit([path, None, str(error)])
    emit({'complete': len(rows)})


if __name__ == '__main__':
    main()
