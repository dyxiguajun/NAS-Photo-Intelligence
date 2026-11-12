"""Resume an immutable queue of existing NAS evidence jobs on the local scratch volume."""
import argparse
from collections import Counter
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import time

from nas_evidence_worker import canonical, chunks, digest, job, validate_result, write_checkpoint


def checkpoint_summary(stage, manifest, identity):
    expected = list(chunks(manifest['rows']))
    files = sorted(stage.glob('[0-9][0-9][0-9][0-9][0-9].json'))
    if len(files) > len(expected):
        raise ValueError('Unexpected evidence checkpoints')
    result = Counter()
    for number, path in enumerate(files, 1):
        if path.name != f'{number:05}.json':
            raise ValueError('Evidence checkpoint gap')
        batch = json.loads(path.read_bytes())
        validate_result(batch, expected[number-1], identity, number)
        for requested, row in zip(expected[number-1], batch['records']):
            result['observed'] += 1
            if 'sha256' in requested['ops']:
                result['sha256_requested'] += 1
                result['sha256_success' if 'sha256' in row else 'sha256_unresolved'] += 1
    return len(files) == len(expected), dict(result)


def run(queue_path, media_root):
    root = queue_path.parent.parent
    raw = queue_path.read_bytes()
    queue = json.loads(raw)
    queue_id = sha256(raw).hexdigest()
    if raw != canonical(queue) or queue_path.parent.name != queue_id:
        raise ValueError('Queue identity does not match its canonical manifest')
    if digest(Path(__file__)) != queue['scheduler_sha256']:
        raise ValueError('Queue scheduler version differs from the frozen runtime')
    jobs = queue['jobs']
    if len(jobs) != len(set(jobs)):
        raise ValueError('Duplicate jobs in queue')

    with (root / '.sha-queue.lock').open('a') as queue_lock:
        try:
            fcntl.flock(queue_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75
        for identity in jobs:
            if len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
                raise ValueError('Invalid queue job identity')
            stage = root / identity
            path = stage / 'manifest.json'
            manifest, actual = job(path, runtime=stage)
            if actual != identity or manifest['parent_manifest_sha256'] != queue['parent_manifest_sha256']:
                raise ValueError('Job does not belong to the frozen manifest')
            while True:
                complete, counts = checkpoint_summary(stage, manifest, identity)
                if complete:
                    write_checkpoint(queue_path.parent / f'{identity}.json', counts)
                    print(json.dumps({'job':identity, **counts}), flush=True)
                    break
                with (root / '.worker.lock').open('a') as worker_lock:
                    try:
                        fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        time.sleep(15)
                        continue
                runner = [sys.executable, '-B', str(stage/'nas_evidence_worker.py'), 'run',
                          '--manifest', str(path), '--media-root', str(media_root),
                          '--seconds', '1800', '--yield-busy']
                with (stage/'run.log').open('a') as log:
                    process = subprocess.run(runner, stdout=log, stderr=subprocess.PIPE)
                    log.write(process.stderr.decode(errors='replace'))
                legacy_lock_conflict = process.stderr.rstrip().endswith(
                    b'BlockingIOError: [Errno 11] Resource temporarily unavailable')
                if process.returncode not in (0,75) and not legacy_lock_conflict:
                    raise RuntimeError(f'Evidence worker failed for {identity}: exit {process.returncode}')
                if process.returncode:
                    time.sleep(15)
        write_checkpoint(queue_path.parent / 'complete.json',
                         {'queue_sha256':queue_id, 'jobs_validated':len(jobs)})
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('queue', type=Path)
    parser.add_argument('media_root', type=Path)
    args = parser.parse_args()
    sys.exit(run(args.queue.resolve(strict=True), args.media_root))
