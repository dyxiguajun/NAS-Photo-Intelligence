"""Strict intake for versioned matching evidence and explicit human review."""
from collections import Counter
from hashlib import sha256
import json
import re

from .organize_decisions import identity, l0_key, l1_key
from .organize_index import encode
from .organize_index import files as indexed_files

TABLE = """CREATE TABLE IF NOT EXISTS organize_evidence_runs(
    run_id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL)"""
HASH64 = re.compile(r'[0-9a-f]{16}')
OUTCOMES = {'matched','no_candidate_in_scope','unavailable','low_information','ambiguous'}
RELATIONS = {'L0','L1','visual_candidate','confirmed_same_capture','ambiguous','conflict','low_information'}


def revision(row):
    return (*identity(row),row['size'],row['mtime_ns'],row['ctime_ns'])


def digest(value):
    return sha256(encode(value).encode()).hexdigest()


def dimension(value):
    return isinstance(value,list) and len(value)==2 and all(type(x) is int and x>0 for x in value)


def validate_czkawka(edge, a, b, runs, algorithm):
    run = runs.get(a)
    if not run or run['export_digest']!=digest(run['export']):
        raise ValueError('Czkawka export does not match its checkpoint')
    group = run['export']
    if len(group)!=1 or group[0][0]['path']!=run['anchor']:
        raise ValueError('Czkawka edge requires one directly compared anchor')
    aliases = {tuple(item['revision']):item['alias'] for item in run['references']}
    candidate = next((item for item in group[0][1] if item['path']==aliases.get(b)),None)
    distance = edge.get('distance')
    if (candidate is None or type(distance) is not int or distance!=candidate['difference']
            or not 0<=distance<=algorithm['max_difference']):
        raise ValueError('Czkawka candidate or distance is not supported by the export')


def validate(bundle, rows):
    if bundle['schema'] != 'organize-evidence-v1' or not bundle['run_id'] or not bundle['producer']:
        raise ValueError('Unknown evidence schema or identity')
    inputs = bundle['inputs']
    if bundle['inputs_sha256'] != digest(inputs):
        raise ValueError('Input checksum mismatch')
    observed = {revision(row):row for row in rows if row['state']=='present'}
    files, roles = {}, Counter()
    versions = set()
    for item in inputs:
        key = tuple(item['revision'])
        if key not in observed or item['inventory_version']!=observed[key]['version'] or key in files:
            raise ValueError('Input revision, version or uniqueness mismatch')
        declared = item['roles']
        if not declared or len(set(declared))!=len(declared) or not set(declared)<= {'query','reference'}:
            raise ValueError('Invalid input roles')
        roles.update(declared)
        versions.add(item['inventory_version'])
        files[key] = item
    if bundle['expected_counts'] != {
            'queries':roles['query'],'references':roles['reference'],'edges':len(bundle['edges'])}:
        raise ValueError('Evidence counts do not reconcile')
    if set(bundle['scope_versions']) != versions or not bundle.get('scope_description'):
        raise ValueError('Search scope must identify all covered inventory versions')
    outcomes, attached, seen_edges = {}, Counter(), set()
    for entry in bundle['queries']:
        key = tuple(entry['revision'])
        if key not in files or 'query' not in files[key]['roles'] or key in outcomes:
            raise ValueError('Unexpected or repeated query outcome')
        if entry['status'] not in OUTCOMES or not entry.get('reason'):
            raise ValueError('Query outcome requires a reason')
        outcomes[key] = entry['status']
    if len(outcomes) != roles['query']:
        raise ValueError('Query outcomes must account for all declared queries')
    runs = {}
    algorithm = bundle.get('algorithm',{})
    if algorithm.get('metric')=='czkawka_reported_distance':
        if (algorithm.get('implementation')!='Czkawka CLI' or not algorithm.get('version')
                or not algorithm.get('image') or not algorithm.get('profile')
                or type(algorithm.get('max_difference')) is not int or not 0<=algorithm['max_difference']<=40):
            raise ValueError('Czkawka evidence needs a pinned runtime and profile')
        reference_keys = {key for key,item in files.items() if 'reference' in item['roles']}
        for run in bundle['scan_runs']:
            anchor = tuple(run['query'])
            refs = [tuple(item['revision']) for item in run['references']]
            aliases = {item['alias'] for item in run['references']}
            if (anchor not in outcomes or anchor in runs or len(refs)!=len(set(refs))
                    or set(refs)!=reference_keys or len(aliases)!=len(refs)
                    or run['anchor'] in aliases
                    or run['export_digest']!=digest(run['export'])):
                raise ValueError('Czkawka checkpoint does not cover the declared reference scope')
            if bool(run['export']) != (outcomes[anchor]=='matched'):
                raise ValueError('Czkawka outcome disagrees with its exported groups')
            if run['export'] and (len(run['export'])!=1 or run['export'][0][0]['path']!=run['anchor']
                    or not {item['path'] for item in run['export'][0][1]}<=aliases):
                raise ValueError('Czkawka report contains an undeclared comparison')
            runs[anchor] = run
        if runs.keys()!=outcomes.keys():
            raise ValueError('Missing Czkawka query checkpoint')
    for edge in bundle['edges']:
        a,b = tuple(edge['a']),tuple(edge['b'])
        kind = edge['relation']
        if (a not in outcomes or b not in files or 'reference' not in files[b]['roles']
                or a==b or (a,b) in seen_edges or kind not in RELATIONS):
            raise ValueError('Invalid or repeated evidence edge')
        seen_edges.add((a,b))
        attached[a] += 1
        if not edge.get('reason'):
            raise ValueError('Evidence edge requires a reason')
        if kind in ('L0','L1'):
            key = l0_key if kind=='L0' else l1_key
            if key(observed[a]) is None or key(observed[a])!=key(observed[b]):
                raise ValueError('Claimed equality is unsupported by current verified index')
        elif kind=='confirmed_same_capture':
            if bundle['producer']!='human_review' or not edge.get('review_id'):
                raise ValueError('Same-capture confirmation requires an explicit human review ID')
        elif kind=='visual_candidate':
            algorithm = bundle.get('algorithm', {})
            if algorithm.get('metric')=='czkawka_reported_distance':
                validate_czkawka(edge,a,b,runs,algorithm)
                continue
            if (not all(algorithm.get(k) for k in ('implementation','version','pipeline'))
                    or algorithm.get('bits')!=64):
                raise ValueError('Visual candidates require a reproducible algorithm version')
            for key in (a,b):
                record = files[key]
                if (record.get('pipeline')!=algorithm['pipeline']
                        or not all(dimension(record.get(k)) for k in ('original_dimensions','oriented_dimensions','sample_dimensions'))
                        or not all(isinstance(record.get(k),str) and HASH64.fullmatch(record[k]) for k in ('phash','dhash'))):
                    raise ValueError('Visual fingerprint preprocessing, dimensions or identity is invalid')
                e = observed[key]['evidence']
                if e.get('width') and e.get('height') and record['original_dimensions'] != [e['width'],e['height']]:
                    raise ValueError('Original dimensions disagree with the indexed metadata')
            for name in ('phash','dhash'):
                calculated = (int(files[a][name],16)^int(files[b][name],16)).bit_count()
                if edge.get(name+'_distance')!=calculated:
                    raise ValueError('Reported fingerprint distance does not match')
    for key,status in outcomes.items():
        if (status=='matched' and not attached[key]) or (status in ('no_candidate_in_scope','unavailable') and attached[key]):
            raise ValueError('Query outcomes conflict with edge coverage')
    ranks = {}
    for edge in bundle['edges']:
        if edge['relation']=='visual_candidate':
            ranks.setdefault(tuple(edge['a']),[]).append(edge.get('rank'))
    if ranks:
        limit = bundle.get('top_k')
        if type(limit) is not int or not 1<=limit<=100 or any(
                len(values)>limit or any(type(x) is not int for x in values)
                or sorted(values)!=list(range(1,len(values)+1)) for values in ranks.values()):
            raise ValueError('Visual rankings must be contiguous and bounded by Top-K')
    return {'inputs':len(inputs),'queries':len(outcomes),'edges':len(seen_edges),
            'outcomes':dict(Counter(outcomes.values()))}


def ingest(db, bundle):
    db.execute('BEGIN IMMEDIATE')
    with db:
        db.execute(TABLE)
        run_id, value = bundle['run_id'], digest(bundle)
        old = db.execute('SELECT digest FROM organize_evidence_runs WHERE run_id=?',(run_id,)).fetchone()
        if old:
            if old[0]!=value:
                raise ValueError('Evidence run ID is already assigned to different content')
            return {'run_id':run_id,'digest':value,'reused':True}
        summary = validate(bundle,indexed_files(db))
        db.execute('INSERT INTO organize_evidence_runs VALUES(?,?,?)',(run_id,value,encode(bundle)))
        return {'run_id':run_id,'digest':value,'reused':False,**summary}


def current(db, rows):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='organize_evidence_runs'").fetchone():
        return [],[],[]
    revisions = {revision(row) for row in rows if row['state']=='present'}
    edges, queries, stale = [],[],[]
    for row in db.execute('SELECT payload FROM organize_evidence_runs ORDER BY run_id'):
        bundle = json.loads(row[0])
        if any(tuple(x['revision']) not in revisions for x in bundle['inputs']):
            stale.append(bundle['run_id'])
            continue
        for edge in bundle['edges']:
            edges.append({**edge,'run_id':bundle['run_id'],'scope':bundle['scope_description'],
                          'execution_allowed':False})
        for query in bundle['queries']:
            queries.append({**query,'run_id':bundle['run_id'],'scope':bundle['scope_description']})
    return edges,queries,stale
