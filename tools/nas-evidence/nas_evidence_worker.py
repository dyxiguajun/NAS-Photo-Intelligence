"""Resumable source-side SHA-256 and ExifTool observations. Stdlib only."""
import argparse
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile
import time

from nas_worker import identity, read_one

PROFILE = 'native-sha256-exiftool-fast2-v1'
LARGE_SHA_PROFILE = 'native-sha256-large-file-v1'
MAX_ROWS = 50_000
MAX_JOB_BYTES = 32 * 1024**3
CHUNK_ROWS = 128
CHUNK_BYTES = 256 * 1024**2
FILE_BYTES = CHUNK_BYTES
LARGE_FILE_BYTES = 8 * 1024**3
LARGE_CHUNK_TIMEOUT = 900
EXIF_FIELDS = ('ImageWidth', 'ImageHeight', 'DateTimeOriginal', 'OffsetTimeOriginal',
               'Make', 'Model', 'LensModel', 'GPSLatitude', 'GPSLongitude', 'Error', 'Warning')


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def write_checkpoint(path, value):
    """Publish only complete results, including after a process restart."""
    data = canonical(value)

    def verify_existing():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise ValueError(f'Checkpoint content conflict: {path}')

    if path.exists() or path.is_symlink():
        verify_existing()
        return
    with NamedTemporaryFile(prefix='.checkpoint-',dir=path.parent) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        try:
            os.link(handle.name,path)
        except FileExistsError:
            verify_existing()
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def safe_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or name == '.' or '..' in path.parts or '\0' in name:
        raise ValueError('Expected a normalized source-relative path')
    return path


def job(path, *, runtime=None):
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if raw != canonical(manifest):
        raise ValueError('Manifest is not in canonical form')
    runtime = Path(__file__).parent if runtime is None else runtime
    if (manifest['profile'] not in (PROFILE,LARGE_SHA_PROFILE)
            or manifest['runner_sha256'] != digest(runtime/'nas_evidence_worker.py')):
        raise ValueError('Manifest runner version mismatch')
    if manifest['reader_sha256'] != digest(runtime/'nas_worker.py'):
        raise ValueError('Manifest SHA reader version mismatch')
    safe_path(manifest['logical_root'])
    if safe_path(manifest['source_id']).parts != (manifest['source_id'],):
        raise ValueError('Invalid source identity')
    rows = manifest['rows']
    large = manifest['profile']==LARGE_SHA_PROFILE
    limit = LARGE_FILE_BYTES if large else FILE_BYTES
    seen = set()
    total = 0
    for row in rows:
        safe_path(row['path'])
        size, mtime_ns, ctime_ns = row['stat']
        if (any(type(n) is not int for n in (size, mtime_ns, ctime_ns)) or not 0 <= size <= limit
                or row['path'] in seen or not row['ops'] or sorted(set(row['ops'])) != row['ops']
                or set(row['ops']) - {'sha256', 'metadata'}
                or (large and (size<=FILE_BYTES or row['ops']!=['sha256']))):
            raise ValueError('Invalid or repeated manifest row')
        seen.add(row['path'])
        total += size
    if not 0 < len(rows) <= MAX_ROWS or total > MAX_JOB_BYTES:
        raise ValueError('Job exceeds row or byte budget')
    return manifest, sha256(raw).hexdigest()


def chunks(rows):
    items, total = [], 0
    for row in rows:
        size = row['stat'][0]
        if items and (len(items) == CHUNK_ROWS or total + size > CHUNK_BYTES):
            yield items
            items, total = [], 0
        items.append(row)
        total += size
    if items:
        yield items


def revision(name, expected):
    path = Path(name)
    if path.resolve(strict=True) != Path.cwd() / path:
        raise ValueError('Unsafe source path')
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or identity(info) != tuple(expected):
        raise ValueError('Source revision changed')
    return info.st_dev, info.st_ino


def capture(rows, exiftool):
    records, metadata_names, inode = [], [], {}
    for row in rows:
        name, expected = row['path'], row['stat']
        record = {'path': name, 'stat': expected, 'errors': {}}
        records.append(record)
        try:
            inode[name] = revision(name, expected)
        except (ValueError, OSError) as error:
            record['errors']['identity'] = str(error)
            continue
        if 'sha256' in row['ops']:
            try:
                record['sha256'] = read_one(name, tuple(expected), False, None)
            except (ValueError, OSError, AssertionError) as error:
                record['errors']['sha256'] = str(error) or 'Source SHA read rejected'
        if 'metadata' in row['ops']:
            metadata_names.append('./' + name)
    metadata = {}
    if metadata_names:
        process = subprocess.run(
            [exiftool, '-json', '-G1', '-n', '-fast2', *('-' + tag for tag in EXIF_FIELDS), *metadata_names],
            capture_output=True, timeout=40)
        if process.returncode not in (0, 1) or not process.stdout:
            raise RuntimeError('ExifTool failed to return a batch')
        for item in json.loads(process.stdout):
            name = item['SourceFile']
            if name.startswith('./'):
                name = name[2:]
            if './' + name not in metadata_names or name in metadata:
                raise ValueError('Unexpected ExifTool result path')
            metadata[name] = item
    for record, row in zip(records, rows):
        name, expected = record['path'], record['stat']
        if name not in inode:
            continue
        try:
            if revision(name, expected) != inode[name]:
                raise ValueError('Source identity changed during capture')
        except (ValueError, OSError) as error:
            record.pop('sha256', None)
            record['errors']['identity'] = str(error)
            continue
        if 'metadata' in row['ops']:
            if name in metadata:
                record['exif'] = metadata[name]
            else:
                record['errors']['metadata'] = 'Missing ExifTool result'
    return records


def validate_result(result, batch, job_id, number):
    if result['manifest_sha256'] != job_id or result['chunk'] != number:
        raise ValueError('Checkpoint input identity mismatch')
    if len(result['records']) != len(batch):
        raise ValueError('Incomplete checkpoint')
    for row, record in zip(batch, result['records']):
        if (row['path'], row['stat']) != (record['path'], record['stat']):
            raise ValueError('Checkpoint source revision mismatch')
        errors = record['errors']
        if any(not isinstance(error, str) or not error for error in errors.values()):
            raise ValueError('Malformed error result')
        if 'identity' in errors and ('sha256' in record or 'exif' in record):
            raise ValueError('Result for a changed source')
        for op, key in (('sha256', 'sha256'), ('metadata', 'exif')):
            if (key in record or op in errors) and op not in row['ops']:
                raise ValueError('Unrequested observation')
            if op in row['ops'] and 'identity' not in errors and (key in record) == (op in errors):
                raise ValueError('Missing or ambiguous observation')
        if 'sha256' in record and (len(record['sha256']) != 64
                                   or any(c not in '0123456789abcdef' for c in record['sha256'])):
            raise ValueError('Malformed SHA-256')
        if 'exif' in record and record['exif']['SourceFile'] not in (row['path'], './'+row['path']):
            raise ValueError('ExifTool result path mismatch')


def is_busy():
    # DSH currently streams tar from the media HDD. Yield whenever it is active.
    for proc in Path('/proc').glob('[0-9]*/comm'):
        try:
            if proc.read_text().strip() in ('tar', 'czkawka_cli', 'ffmpeg'):
                return True
        except OSError:
            continue
    return False


def run(args):
    path = args.manifest.resolve(strict=True)
    manifest, job_id = job(path)
    media = args.media_root.resolve(strict=True)
    scope = media / manifest['logical_root']
    scratch = path.parent
    if not scope.is_dir() or scope.resolve() != scope or scratch == media or media in scratch.parents:
        raise ValueError('Unsafe source or output root')
    if scratch.name != job_id:
        raise ValueError('Job directory must match the manifest digest')
    started = time.monotonic()
    large = manifest['profile']==LARGE_SHA_PROFILE
    lock_fd = os.open(scratch.parent / '.worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('NAS evidence workspace already has an active worker', file=sys.stderr, flush=True)
            return 75
        if not large:
            observed = subprocess.check_output([args.exiftool, '-ver'], text=True, timeout=10).strip()
            if observed != manifest['exiftool_version']:
                raise ValueError(f'ExifTool version differs from manifest: {observed}')
        for number, batch in enumerate(chunks(manifest['rows']), 1):
            target = scratch / f'{number:05}.json'
            if target.exists():
                validate_result(json.loads(target.read_bytes()), batch, job_id, number)
                continue
            while args.yield_busy and is_busy() and time.monotonic() - started < args.seconds:
                time.sleep(5)
            if time.monotonic() - started >= args.seconds:
                print(f'Paused before chunk {number}', flush=True)
                return
            tick = time.monotonic()
            process = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).resolve()), 'capture', '--exiftool', args.exiftool],
                cwd=scope, input=canonical(batch), capture_output=True, timeout=LARGE_CHUNK_TIMEOUT if large else 80)
            if process.returncode:
                raise RuntimeError(f'Chunk {number} did not commit: ' + process.stderr.decode(errors='replace')[-1000:])
            result = {'manifest_sha256': job_id, 'chunk': number, 'records': json.loads(process.stdout)}
            validate_result(result, batch, job_id, number)
            write_checkpoint(target, result)
            print(json.dumps({'chunk': number, 'rows': len(batch), 'bytes': sum(r['stat'][0] for r in batch),
                              'seconds': round(time.monotonic()-tick, 3),
                              'files_with_errors': sum(bool(r['errors']) for r in result['records'])}), flush=True)
    print(f'Complete {job_id}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run','capture'))
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--media-root', type=Path)
    parser.add_argument('--exiftool', default='exiftool')
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--yield-busy', action='store_true')
    args = parser.parse_args()
    if args.action == 'capture':
        print(canonical(capture(json.load(sys.stdin), args.exiftool)).decode(), end='')
    elif args.manifest is None or args.media_root is None or not 1 <= args.seconds <= 3600:
        parser.error('run requires a manifest, media root and a 1..3600 second scheduling window')
    else:
        sys.exit(run(args))


if __name__ == '__main__':
    main()
