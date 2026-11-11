"""Look up requested byte identities within a frozen, read-only inventory scope."""
from collections import Counter
from hashlib import sha256
import json

from .organize_index import SHA256


def exact_matches(db, frozen_path, frozen_sha256, objects_path):
    data = frozen_path.read_bytes()
    if sha256(data).hexdigest()!=frozen_sha256:
        raise ValueError('Frozen inventory checksum mismatch')
    frozen = {}
    for line in data.splitlines():
        item = json.loads(line)
        key = tuple(item['source'])
        if key in frozen:
            raise ValueError('Repeated frozen source identity')
        frozen[key] = tuple(item['stat']) if item['state']=='present' else None
    objects = objects_path.read_bytes()
    requests = {}
    for line in objects.splitlines():
        row = json.loads(line)
        digest,size = row['object_sha256'],row['length']
        if (not isinstance(digest,str) or not SHA256.fullmatch(digest) or type(size) is not int
                or size<0 or digest in requests):
            raise ValueError('Malformed or repeated requested SHA-256')
        requests[digest] = {'object_sha256':digest,'length':size,'matching_source_paths':0,
                            'matching_sources':set()}
    if not requests:
        raise ValueError('No requested objects')
    observed = 0
    for sid,path,size,mtime_ns,ctime_ns,digest in db.execute(
        """SELECT source_id,relative_path,size,mtime_ns,ctime_ns,json_extract(evidence,'$.sha256')
           FROM organize_files WHERE state='present' AND json_type(evidence,'$.sha256')='text'"""):
        if frozen.get((sid,path))!=(size,mtime_ns,ctime_ns) or not SHA256.fullmatch(digest):
            continue
        observed += 1
        query = requests.get(digest)
        if query and query['length']==size:
            query['matching_source_paths'] += 1
            query['matching_sources'].add(sid)
    counts = Counter()
    for row in requests.values():
        row['matching_sources'] = sorted(row['matching_sources'])
        row['result'] = ('exact_match_in_verified_scope' if row['matching_source_paths']
                         else 'no_exact_match_in_verified_scope')
        counts[row['result']] += 1
    return {'inventory_manifest_sha256':frozen_sha256,'inventory_denominator':len(frozen),
            'inventory_sha_verified_at_frozen_revision':observed,'query_sha256':sha256(objects).hexdigest(),
            'query_denominator':len(requests),'counts':dict(counts),'objects':list(requests.values()),
            'execution_allowed':False}
