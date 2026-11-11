"""Compare versioned snapshots without contacting or modifying either storage endpoint."""
from collections import Counter, defaultdict

from .config import relative
from .organize_index import SHA256


def validate(manifest):
    if not manifest['scope'] or not manifest['version'] or type(manifest['complete']) is not bool:
        raise ValueError('A scope, version and coverage flag are required')
    files = {}
    for row in manifest['files']:
        path, size, digest = row['path'], row['size'], row.get('sha256')
        if relative(path) != path or path in files or type(size) is not int or size < 0:
            raise ValueError('Invalid or repeated manifest file')
        if digest is not None and (not isinstance(digest,str) or not SHA256.fullmatch(digest)):
            raise ValueError('Invalid manifest SHA-256')
        files[path] = (size,digest)
    return files


def reconcile(source, destination):
    left, right = validate(source), validate(destination)
    reusable = defaultdict(list)
    for path, key in sorted(right.items()):
        if key[1] is not None:
            reusable[key].append(path)
    rows = []
    count = Counter()
    bytes_by_state = Counter()
    for path in sorted(left.keys() | right.keys()):
        local, remote = left.get(path), right.get(path)
        if local is None:
            state = 'destination_only' if source['complete'] else 'source_not_observed'
        elif remote is None:
            state = 'missing_at_destination' if destination['complete'] else 'destination_not_observed'
        elif local[0] != remote[0]:
            state = 'size_mismatch'
        elif local[1] is None or remote[1] is None:
            state = 'same_size_sha_unverified'
        else:
            state = 'verified_equal' if local[1]==remote[1] else 'sha_mismatch'
        copies = reusable.get(local, []) if state != 'verified_equal' else []
        rows.append({'path':path, 'state':state,'source':local,'destination':remote,
                     'reusable_destination_paths':copies,'execution_allowed':False})
        count[state] += 1
        if local:
            bytes_by_state[state] += local[0]
    return {'source':{k:source[k] for k in ('scope','version','complete')},
            'destination':{k:destination[k] for k in ('scope','version','complete')},
            'source_paths':len(left),'destination_paths':len(right),'counts':count,
            'source_bytes_by_state':bytes_by_state,'rows':rows,'execution_allowed':False,
            'all_content_verified':source['complete'] and destination['complete']
                                   and count['verified_equal']==len(rows)}
