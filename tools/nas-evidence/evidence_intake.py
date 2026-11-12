"""Plan and ingest checkpoint evidence without NAS paths or credentials."""
from collections import Counter
import json
from pathlib import Path

from nas_photo.organize_index import apply_batch, normalize
from nas_evidence_worker import FILE_BYTES, LARGE_FILE_BYTES, LARGE_SHA_PROFILE, PROFILE, chunks, digest, validate_result

WORKER = Path(__file__).with_name('nas_evidence_worker.py')
READER = WORKER.with_name('nas_worker.py')


def metadata_evidence(obs, failure):
    tags = {'ExifIFD:DateTimeOriginal':'date_time_original','ExifIFD:OffsetTimeOriginal':'offset_time_original',
            'IFD0:Make':'make','IFD0:Model':'model','ExifIFD:LensModel':'lens_model'}
    if failure or obs.get('ExifTool:Error'):
        return {'metadata_error':failure or obs['ExifTool:Error']}
    metadata = {target:obs[tag] for tag,target in tags.items() if tag in obs}
    if all(k in obs for k in ('Composite:GPSLatitude','Composite:GPSLongitude')):
        metadata['gps'] = [obs['Composite:GPSLatitude'],obs['Composite:GPSLongitude']]
    return {'metadata':metadata,'width':obs.get('File:ImageWidth'),'height':obs.get('File:ImageHeight')}


def plan(db, source, max_rows, max_bytes, exiftool_version, all_sha=False, *, large_sha=False):
    scope = db.execute('SELECT * FROM organize_sources WHERE source_id=?', (source,)).fetchone()
    if not scope:
        raise ValueError('Source is not registered')
    sizes = ({} if large_sha else
             Counter(dict(db.execute("SELECT size,count(*) FROM organize_files WHERE state='present' GROUP BY size"))))
    manifest = dict(scope)
    manifest.update(profile=LARGE_SHA_PROFILE if large_sha else PROFILE,
                    exiftool_version=None if large_sha else exiftool_version, runner_sha256=digest(WORKER),
                    reader_sha256=digest(READER), rows=[])
    selected_bytes = 0
    # Cover small eligible files first, retaining the original stable order within a size.
    if large_sha:
        where = "size>? AND size<=? AND json_type(evidence,'$.sha256') IS NULL"
        bounds = (FILE_BYTES,LARGE_FILE_BYTES)
    else:
        where = """size<=? AND (json_type(evidence,'$.sha256') IS NULL OR
                (json_type(evidence,'$.metadata') IS NULL AND json_type(evidence,'$.metadata_error') IS NULL))"""
        bounds = (FILE_BYTES,)
    for path, size, mt, ct, evidence in db.execute(
            "SELECT relative_path,size,mtime_ns,ctime_ns,evidence FROM organize_files "
            f"WHERE source_id=? AND state='present' AND {where} ORDER BY size,relative_path",(source,*bounds)):
        observed = json.loads(evidence)
        ops = []
        if not large_sha and 'metadata' not in observed and 'metadata_error' not in observed:
            ops.append('metadata')
        if not observed.get('sha256') and (large_sha or all_sha or sizes[size] > 1):
            ops.append('sha256')
        if not ops:
            continue
        if selected_bytes + size > max_bytes:
            break
        manifest['rows'].append({'path':path, 'stat':[size,mt,ct], 'ops':ops})
        selected_bytes += size
        if len(manifest['rows']) == max_rows:
            break
    if not manifest['rows']:
        raise ValueError('No pending observations fit the selected source and budget')
    return manifest


def observations(result):
    rows = []
    for record in result['records']:
        exif = record.get('exif')
        evidence = {'sha256':record['sha256']} if 'sha256' in record else {}
        if exif is not None:
            observation = metadata_evidence(exif, None)
            if 'metadata' in observation:
                for field in ('width','height'):
                    tag = 'Image' + field.title()
                    # The same ExifTool tag can be in File, PNG, QuickTime, etc.
                    values = {v for k,v in exif.items() if k.endswith(':'+tag) and type(v) is int and v > 0}
                    if len(values) == 1:
                        observation[field] = values.pop()
            evidence.update(observation)
        if evidence:
            row = {'path':record['path'],'stat':record['stat'],'evidence':evidence}
            normalize(row)
            rows.append(row)
    return rows


def verified_checkpoints(received, manifest, job_id):
    """Accept a complete prefix of verified chunks from a running or stopped job."""
    expected = list(chunks(manifest['rows']))
    checked = []
    for number,(name,result) in enumerate(received,1):
        if number>len(expected) or name!=f'{number:05}.json':
            raise ValueError('Non-contiguous or unexpected checkpoint')
        validate_result(result,expected[number-1],job_id,number)
        checked.append((name,result))
    return checked


def import_results(db, path, manifest, job_id):
    expected = len(list(chunks(manifest['rows'])))
    parsed, counters, commits = [], Counter(), []
    scope = db.execute('SELECT * FROM organize_sources WHERE source_id=?', (manifest['source_id'],)).fetchone()
    if not scope or any(scope[k] != manifest[k] for k in ('device_id','logical_root')):
        raise ValueError('Job source identity differs from the registered source')
    files = sorted(path.parent.glob('[0-9][0-9][0-9][0-9][0-9].json'))
    received = ((checkpoint.name,json.loads(checkpoint.read_bytes())) for checkpoint in files)
    for name,data in verified_checkpoints(received,manifest,job_id):
        checkpoint = path.parent/name
        number = data['chunk']
        rows = observations(data)
        parsed.append((number, rows, digest(checkpoint)))
        counters['rows_observed'] += len(data['records'])
        counters['sha256_success'] += sum('sha256' in row['evidence'] for row in rows)
        counters['metadata_success'] += sum('metadata' in row['evidence'] for row in rows)
        counters['metadata_file_error'] += sum('metadata_error' in row['evidence'] for row in rows)
        for record in data['records']:
            for error in record['errors']:
                counters[error+'_read_error'] += 1
    for number, rows, checksum in parsed:
        version = f'nas-evidence-{job_id}-{number:05}-{checksum[:12]}'
        if rows:
            committed = apply_batch(db, manifest['source_id'], version, rows, require_current=True)
            commits.append({'version':version,'checkpoint_sha256':checksum,**committed})
        else:
            commits.append({'version':version,'checkpoint_sha256':checksum,'rows':0})
    return {'source_id':manifest['source_id'],'manifest_sha256':job_id,'rows_planned':len(manifest['rows']),
            'parent_manifest_sha256':manifest.get('parent_manifest_sha256'),
            'chunks_planned':expected,'chunks_collected':len(parsed),'counts':dict(counters),'commits':commits}


