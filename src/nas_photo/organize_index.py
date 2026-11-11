"""Versioned inventory and evidence for an independent organization workspace."""
from collections import defaultdict
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3

from .config import relative

SCHEMA = """
CREATE TABLE IF NOT EXISTS organize_sources(
  source_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, logical_root TEXT NOT NULL,
  UNIQUE(device_id, logical_root)
);
CREATE TABLE IF NOT EXISTS organize_batches(
  batch_id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES organize_sources,
  version TEXT NOT NULL, digest TEXT NOT NULL, coverage TEXT NOT NULL,
  expected_rows INTEGER NOT NULL, UNIQUE(source_id, version)
);
CREATE TABLE IF NOT EXISTS organize_files(
  source_id TEXT NOT NULL REFERENCES organize_sources, relative_path TEXT NOT NULL,
  size INTEGER, mtime_ns INTEGER, ctime_ns INTEGER,
  state TEXT NOT NULL, change_kind TEXT NOT NULL, evidence TEXT NOT NULL,
  observed_batch INTEGER NOT NULL REFERENCES organize_batches, issue TEXT,
  PRIMARY KEY(source_id, relative_path)
);
CREATE INDEX IF NOT EXISTS organize_files_size ON organize_files(size);
CREATE TABLE IF NOT EXISTS organize_stale_evidence(
  batch_id INTEGER NOT NULL REFERENCES organize_batches, relative_path TEXT NOT NULL,
  PRIMARY KEY(batch_id, relative_path)
);
"""
SHA256 = re.compile(r'[a-f0-9]{64}')


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


@contextmanager
def connect(path, *, write=False):
    path = Path(path).resolve()
    if write:
        db = sqlite3.connect(path)
    else:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        if write:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'organize_sources' not in tables:
                columns = {r[1] for r in db.execute('PRAGMA table_info(files)')}
                if not {'l0_attempts', 'batches'} <= tables or not {'relpath', 'exif_dt', 'batch'} <= columns:
                    raise ValueError('Expected the audit analysis database or a new isolated SQLite file')
            db.executescript(SCHEMA)
        yield db
    finally:
        db.close()


def register_sources(db, sources):
    with db:
        for row in sources:
            sid, device, root = row['source_id'], row['device_id'], relative(row['logical_root'], True)
            if not sid or relative(sid) != sid or '/' in sid or not device:
                raise ValueError('Invalid source identity')
            db.execute('INSERT OR IGNORE INTO organize_sources VALUES(?,?,?)', (sid, device, root))
            actual = db.execute('SELECT device_id,logical_root FROM organize_sources WHERE source_id=?', (sid,)).fetchone()
            if actual is None or tuple(actual) != (device, root):
                raise ValueError(f'Source identity conflicts: {sid}')


def normalize(item):
    path = item['path']
    if relative(path) != path:
        raise ValueError('Inventory requires normalized relative paths')
    state = item.get('state', 'present')
    issue = item.get('issue')
    if state == 'unavailable':
        if not issue:
            raise ValueError('Unavailable observation requires a reason')
        return path, state, None, {}, str(issue)
    if state != 'present':
        raise ValueError('Inventory accepts present or unavailable observations')
    identity = item['stat']
    if len(identity) != 3 or any(type(n) is not int for n in identity) or identity[0] < 0:
        raise ValueError('Expected size, mtime_ns and ctime_ns')
    evidence = item.get('evidence', {})
    if not isinstance(evidence, dict):
        raise ValueError('Invalid evidence object')
    if 'sha256' in evidence and not SHA256.fullmatch(evidence['sha256']):
        raise ValueError('Invalid SHA-256')
    return path, state, tuple(identity), evidence, issue


def apply_batch(db, source, version, rows, *, coverage='partial', require_current=False):
    """Commit a complete captured batch atomically. Exact replay is a no-op."""
    if coverage not in ('partial', 'complete') or not version:
        raise ValueError('Invalid inventory coverage or version')
    rows = sorted((normalize(row) for row in rows), key=lambda row: row[0])
    if len({row[0] for row in rows}) != len(rows):
        raise ValueError('Duplicate relative path in inventory batch')
    if require_current and (coverage != 'partial' or any(row[1] != 'present' for row in rows)):
        raise ValueError('Asynchronous evidence requires present revisions and partial coverage')
    batch_key = [source, version, coverage, rows]
    if require_current:
        batch_key.append('require_current')
    digest = sha256(encode(batch_key).encode()).hexdigest()
    db.execute('BEGIN IMMEDIATE')
    with db:
        previous = db.execute('SELECT batch_id,digest FROM organize_batches WHERE source_id=? AND version=?',
                              (source, version)).fetchone()
        if previous:
            if previous['digest'] != digest:
                raise ValueError('Inventory version already committed with different content')
            result = {'batch_id': previous['batch_id'], 'reused': True, 'rows': len(rows), 'digest': digest}
            if require_current:
                result['stale_paths'] = [r[0] for r in db.execute(
                    'SELECT relative_path FROM organize_stale_evidence WHERE batch_id=? ORDER BY relative_path',
                    (previous['batch_id'],))]
            return result
        existing = {row['relative_path']: row for row in db.execute(
            'SELECT * FROM organize_files WHERE source_id=?', (source,))} if coverage == 'complete' else None
        batch = db.execute('INSERT INTO organize_batches(source_id,version,digest,coverage,expected_rows) VALUES(?,?,?,?,?)',
                           (source, version, digest, 'evidence' if require_current else coverage, len(rows))).lastrowid
        stale_paths = []
        for path, state, identity, evidence, issue in rows:
            old = (existing.pop(path, None) if existing is not None else db.execute(
                'SELECT * FROM organize_files WHERE source_id=? AND relative_path=?', (source, path)).fetchone())
            prior_stat = tuple(old[k] for k in ('size','mtime_ns','ctime_ns')) if old else (None,None,None)
            if require_current and (prior_stat != identity or old['state'] != 'present'):
                stale_paths.append(path)
                db.execute('INSERT INTO organize_stale_evidence VALUES(?,?)', (batch,path))
                continue
            if state == 'unavailable':
                identity = prior_stat
                evidence = json.loads(old['evidence']) if old else {}
                change = 'unavailable'
            elif old and prior_stat == identity:
                prior_evidence = json.loads(old['evidence'])
                if 'sha256' in prior_evidence and 'sha256' in evidence and prior_evidence['sha256'] != evidence['sha256']:
                    raise ValueError(f'Conflicting SHA-256 for unchanged identity: {path}')
                if require_current and 'metadata' in prior_evidence:
                    evidence = {key: value for key, value in evidence.items()
                                if key not in ('metadata','metadata_error','width','height')}
                if 'metadata' in evidence and 'metadata_error' not in evidence:
                    prior_evidence.pop('metadata_error', None)
                evidence = {**prior_evidence, **evidence}
                change = 'unchanged' if old['state'] == 'present' else 'reappeared'
            else:
                change = 'changed' if old else 'new'
            if require_current:
                db.execute('UPDATE organize_files SET evidence=? WHERE source_id=? AND relative_path=?',
                           (encode(evidence), source, path))
                continue
            db.execute('''INSERT INTO organize_files VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id,relative_path) DO UPDATE SET
                size=excluded.size,mtime_ns=excluded.mtime_ns,ctime_ns=excluded.ctime_ns,
                state=excluded.state,change_kind=excluded.change_kind,evidence=excluded.evidence,
                observed_batch=excluded.observed_batch,issue=excluded.issue''',
                (source, path, *identity, state, change, encode(evidence), batch, issue))
        if coverage == 'complete':
            db.executemany('''UPDATE organize_files SET state='missing',change_kind='missing',
                observed_batch=?,issue='absent_from_complete_inventory'
                WHERE source_id=? AND relative_path=?''', ((batch, source, path) for path in existing))
    result = {'batch_id': batch, 'reused': False, 'rows': len(rows), 'digest': digest}
    if require_current:
        result['stale_paths'] = stale_paths
    return result


def files(db):
    result = []
    for row in db.execute('''SELECT f.*,b.version FROM organize_files f JOIN organize_batches b
                    ON b.batch_id=f.observed_batch ORDER BY f.source_id,f.relative_path'''):
        item = dict(row)
        item['evidence'] = json.loads(item['evidence']) if item['state'] == 'present' else {}
        result.append(item)
    return result


def status(db, rows=None):
    rows = files(db) if rows is None else rows
    sources = {}
    sha_groups = defaultdict(list)
    same_size = defaultdict(list)
    for source in db.execute('SELECT * FROM organize_sources ORDER BY source_id'):
        item = dict(source)
        item.update(registered=0,present=0,missing=0,unavailable=0,metadata_processed=0,
                    metadata_pending=0,metadata_failed=0,sha_verified=0,l0_duplicate_paths=0,
                    l0_pending=0,changed=0)
        item['batches'] = [dict(row) for row in db.execute(
            'SELECT version,digest,coverage,expected_rows FROM organize_batches WHERE source_id=? ORDER BY batch_id',
            (source['source_id'],))]
        sources[source['source_id']] = item
    for row in rows:
        s = sources[row['source_id']]
        s['registered'] += 1
        s[row['state']] += 1
        s['changed'] += row['change_kind'] == 'changed'
        if row['state'] != 'present':
            continue
        e = row['evidence']
        s['metadata_failed' if e.get('metadata_error') else 'metadata_processed' if 'metadata' in e else 'metadata_pending'] += 1
        same_size[row['size']].append(row)
        if e.get('sha256'):
            sha_groups[(row['size'], e['sha256'])].append(row)
            s['sha_verified'] += 1
    for group in sha_groups.values():
        if len(group) > 1:
            for row in group:
                sources[row['source_id']]['l0_duplicate_paths'] += 1
    for group in same_size.values():
        if len(group) > 1:
            for row in group:
                sources[row['source_id']]['l0_pending'] += not row['evidence'].get('sha256')
    return {'sources': sources, 'registered':len(rows),
            'cross_source_l0_groups':sum(len({r['source_id'] for r in group}) > 1
                                         for group in sha_groups.values())}
