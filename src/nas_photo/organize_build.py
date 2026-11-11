"""Seal a conservative, hash-backed copy preflight and rehearse it in isolation."""
from collections import Counter, defaultdict
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .metadata import secure_path
from .organize_decisions import destinations, identity, l0_key, quality
from .organize_exercise import checksum, verified_copy
from .organize_index import encode


def prepare(rows, disposition, output):
    """Check every frozen source revision and classify every destination."""
    if output.exists():
        raise FileExistsError('Build preflight already exists')
    current = {tuple(identity(row)):row for row in rows}
    _, targets = destinations(row for row in rows if row['state']=='present')
    exact = defaultdict(list)
    for row in rows:
        if row['state']=='present' and l0_key(row):
            exact[l0_key(row)].append(row)
    representative = {}
    for key, members in exact.items():
        if len(members)>1:
            representative[key] = min(members,key=lambda row: (
                not bool(row['evidence'].get('recovery_container')),tuple(-v for v in quality(row)),identity(row)))
    counts, volume, largest = Counter(), Counter(), 0
    ledger_counts = Counter()
    input_digest = sha256()
    output.parent.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(prefix='.build-preflight-',dir=output.parent) as tmp:
        staged = Path(tmp)/'preflight'
        staged.mkdir()
        with (staged/'frozen-manifest.jsonl').open('wb') as frozen:
            for key,row in sorted(current.items()):
                frozen.write((encode({'source':list(key),'state':row['state'],
                                      'stat':[row[k] for k in ('size','mtime_ns','ctime_ns')]})+'\n').encode())
            frozen.flush()
            os.fsync(frozen.fileno())
        manifest_sha = checksum(staged/'frozen-manifest.jsonl')
        with ((staged/'ready.jsonl').open('wb') as ready, (staged/'blocked.jsonl').open('wb') as blocked,
              (staged/'l0-excluded.jsonl').open('wb') as excluded):
            with disposition.open('rb') as source:
                for line in source:
                    input_digest.update(line)
                    item = json.loads(line)
                    key = tuple(item['source'])
                    original = current.pop(key,None)
                    if not original:
                        raise ValueError('Disposition contains an unknown or repeated file identity')
                    revision = [original[k] for k in ('size','mtime_ns','ctime_ns')]
                    sha = original['evidence'].get('sha256')
                    if (item['source_stat']!=revision or item['sha256']!=sha
                            or item['state']!=original['state'] or item['observed_version']!=original['version']
                            or item['proposed_destination']!=targets.get(key) or item['size']!=revision[0]):
                        raise ValueError('Disposition does not match the current frozen inventory')
                    keeper = representative.get(l0_key(original))
                    protection = original['evidence'].get('recovery_container')
                    replacement = None
                    if keeper is not None and identity(keeper)!=list(key) and not protection:
                        replacement = {'source':identity(keeper),'destination':targets[tuple(identity(keeper))],
                                       'stat':[keeper[k] for k in ('size','mtime_ns','ctime_ns')],
                                       'sha256':keeper['evidence']['sha256']}
                    status = ('source_not_present' if item['state']!='present' else
                              'exact_copy_represented' if replacement else
                              'sha256_pending' if not sha else 'ready_for_copy_review')
                    disposition_state = ('excluded' if replacement else 'review' if protection or
                                         status!='ready_for_copy_review' else 'processed')
                    counts[status] += 1
                    ledger_counts[disposition_state] += 1
                    volume[status] += item['size'] or 0
                    record = {'source':list(key),'stat':revision,'inventory_version':original['version'],
                              'destination':targets.get(key),'sha256':sha,'status':status,
                              'ledger_state':disposition_state,'execution_allowed':False}
                    if replacement:
                        record['replacement'] = replacement
                        record['reason'] = 'same_size_and_sha256_at_frozen_revision'
                        stream = excluded
                    elif status == 'ready_for_copy_review':
                        largest = max(largest,item['size'])
                        stream = ready
                    else:
                        stream = blocked
                    if protection:
                        record['recovery_protection'] = protection
                    stream.write((encode(record)+'\n').encode())
            for stream in (ready,blocked,excluded):
                stream.flush()
                os.fsync(stream.fileno())
        if current:
            raise ValueError('Disposition omitted source identities')
        summary = {'policy':'l0_representatives_with_visual_annotations','execution_allowed':False,
                   'inventory_rows':sum(counts.values()),'counts':dict(counts),'logical_bytes':dict(volume),
                   'ledger_states':dict(ledger_counts),'manifest_sha256':manifest_sha,
                   'planned_library_bytes':sum(size for name,size in volume.items() if name!='exact_copy_represented'),
                   'peak_single_staging_file_bytes':largest,'disposition_sha256':input_digest.hexdigest(),
                   'outputs_sha256':{p.name:checksum(p) for p in sorted(staged.glob('*.jsonl'))}}
        (staged/'summary.json').write_text(encode(summary)+'\n')
        if output.exists():
            raise FileExistsError('Build preflight destination appeared')
        staged.rename(output)
    return summary


def _verified_fixture_batch(manifest, scope, library, limit):
    """Run only with internally generated rehearsal roots."""
    rows = [json.loads(line) for line in manifest.read_bytes().splitlines()]
    return [verified_copy(secure_path(scope/row['source'][0], row['source'][1]),library,
                          row['destination'],row['sha256'],expected_stat=row['stat']) for row in rows[:limit]]


def exercise_preflight():
    from .organize_decisions import simulation

    with TemporaryDirectory(prefix='nas-photo-copy-preflight-') as tmp:
        root = Path(tmp)
        fixture = root/'input'
        scope = fixture/'test'
        library = root/'library'
        scope.mkdir(parents=True)
        library.mkdir()
        rows = []
        for name,payload,hashed in (('A.jpg',b'generated 1',True),('b.JPG',b'generated 2',True),
                                    ('duplicate.jpg',b'generated 1',True),
                                    ('retained-without-sha.jpg',b'generated 3',False)):
            path = scope/name
            path.write_bytes(payload)
            st = path.stat()
            rows.append({'source_id':'test','relative_path':name,'size':st.st_size,'mtime_ns':st.st_mtime_ns,
                         'ctime_ns':st.st_ctime_ns,'version':'fixture','state':'present',
                         'evidence':{'sha256':sha256(payload).hexdigest()} if hashed else {}})
        disposition,_,_ = simulation(rows)
        input_file = root/'disposition.jsonl'
        input_file.write_text(''.join(encode(r)+'\n' for r in disposition))
        output = root/'preflight'
        summary = prepare(rows,input_file,output)
        manifest = output/'ready.jsonl'
        first = _verified_fixture_batch(manifest,fixture,library,1)
        resumed = _verified_fixture_batch(manifest,fixture,library,None)
        observed = {p.relative_to(library).as_posix():checksum(p) for p in library.rglob('*') if p.is_file()}
        expected = {row['destination']:row['sha256'] for row in map(json.loads,manifest.read_bytes().splitlines())}
        if observed!=expected or first!=['copied_verified'] or resumed!=['already_verified','copied_verified']:
            raise AssertionError('Preflight execution or restart verification failed')
        if checksum(manifest)!=summary['outputs_sha256']['ready.jsonl']:
            raise AssertionError('Ready list checksum changed')
        duplicate, = map(json.loads,(output/'l0-excluded.jsonl').read_bytes().splitlines())
        if duplicate['replacement']['sha256'] != duplicate['sha256'] or duplicate['status'] != 'exact_copy_represented':
            raise AssertionError('Excluded identity lacks a byte-identical replacement')
    return {'status':'completed','scope':'isolated generated fixtures','preflight':summary,
            'first_batch':first,'resumed_batch':resumed,'entity_manifest_verified':True}
