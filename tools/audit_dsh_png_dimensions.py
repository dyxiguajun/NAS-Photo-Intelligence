"""Audit an externally produced PNG dimension repair against a frozen inventory."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import sqlite3
from tempfile import NamedTemporaryFile

MAX_PNG_DIMENSION = 2**31-1


def load_scan(path):
    raw = path.read_bytes()
    rows = {}
    for line in raw.splitlines():
        record = json.loads(line)
        name = record['p']
        if not isinstance(name,str) or name in rows:
            raise ValueError('Scan contains a missing or repeated path')
        if (type(record['sz']) is not int or record['sz']<0 or type(record['ok']) is not bool
                or not isinstance(record['ext'],str)):
            raise ValueError('Invalid dimension scan record')
        rows[name] = record
    return rows, sha256(raw).hexdigest()


def size(record):
    if record is not None and record['ok']:
        w,h = record['w'],record['h']
        if all(type(n) is int and 0<n<=MAX_PNG_DIMENSION for n in (w,h)):
            return (w,h)
    return None


def eligible(dimensions):
    return dimensions is not None and min(dimensions)>=600


def audit(legacy, corrected, frozen, frozen_sha, sources, alias_root, database=None):
    """Return counts and a disposition per legacy PNG, without promoting dimensions."""
    old,old_sha = load_scan(legacy)
    new,new_sha = load_scan(corrected) if corrected else ({},None)
    data = frozen.read_bytes()
    if sha256(data).hexdigest()!=frozen_sha:
        raise ValueError('Frozen inventory digest mismatch')
    revisions = {}
    roots = sorted(((str(PurePosixPath(alias_root)/src['logical_root'])+'/',src['source_id']) for src in sources),
                   key=lambda entry: -len(entry[0]))
    for line in data.splitlines():
        row = json.loads(line)
        sid,path = row['source']
        key = sid,path
        if key in revisions:
            raise ValueError('Duplicate frozen source identity')
        revisions[key] = tuple(row['stat']) if row['state']=='present' else None

    dimensions = {}
    if database:
        with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
            for sid,path,sz,mt,ct,w,h in db.execute("""SELECT source_id,relative_path,size,mtime_ns,ctime_ns,
                    json_extract(evidence,'$.width'),json_extract(evidence,'$.height')
                    FROM organize_files WHERE state='present' AND lower(relative_path) LIKE '%.png'"""):
                if revisions.get((sid,path))==(sz,mt,ct) and type(w) is int and type(h) is int and min(w,h)>0:
                    dimensions[sid,path] = w,h

    legacy_png = {p for p,r in old.items() if r['ext'].lower()=='png'}
    corrected_png = {p for p,r in new.items() if r['ext'].lower()=='png'}
    counts = Counter()
    ledger = []
    for path in sorted(legacy_png | corrected_png):
        before = old.get(path)
        after = new.get(path)
        key = next(((sid,path[len(prefix):]) for prefix,sid in roots if path.startswith(prefix)),None)
        revision = revisions.get(key)
        old_dim = size(before)
        if old_dim:
            counts['legacy_dimensions_reported'] += 1
            counts['legacy_over_40mp'] += old_dim[0]*old_dim[1]>40_000_000
            counts['legacy_ihdr_sentinel_134348800'] += old_dim[1]==134_348_800
        counts['legacy_hires'] += eligible(old_dim)
        new_dim = None
        if revision is None or (before is not None and revision[0]!=before['sz']):
            status = 'legacy_scope_or_size_mismatch'
        elif not corrected:
            status = 'awaiting_corrected_scan'
        elif after is None:
            status = 'missing_corrected_row'
        elif after['sz']!=revision[0] or after['ext'].lower()!='png':
            status = 'corrected_scope_or_size_mismatch'
        elif (new_dim := size(after)) is None:
            status = 'corrected_dimensions_unavailable'
        else:
            status = 'corrected_dimensions_available'
            counts['corrected_hires'] += eligible(new_dim)
            if before is None:
                counts['additional_hires_without_legacy_row'] += eligible(new_dim)
            else:
                counts['entered_hires'] += eligible(new_dim) and not eligible(old_dim)
            counts['left_hires'] += eligible(old_dim) and not eligible(new_dim)
            counts['corrected_over_40mp'] += new_dim[0]*new_dim[1]>40_000_000
            if old_dim:
                counts['fixed_height_matches_legacy_first_field'] += new_dim[1]==old_dim[0]
            if key in dimensions:
                counts['matches_indexed_metadata' if new_dim==dimensions[key]
                       else 'differs_from_indexed_metadata'] += 1
        counts[status] += 1
        ledger.append({'source':key,'unmapped_path':path if key is None else None,'frozen_stat':revision,
                       'legacy_dimensions':old_dim,'corrected_dimensions':new_dim,
                       'legacy_record_present':before is not None,
                       'legacy_hires_eligible':eligible(old_dim),
                       'corrected_hires_eligible':eligible(new_dim) if new_dim else None,
                       'corrected_over_40mp':new_dim[0]*new_dim[1]>40_000_000 if new_dim else None,
                       'indexed_metadata_dimensions':dimensions.get(key),'status':status,
                       'disposition':'research_coverage_only','execution_allowed':False})

    return {'manifest_sha256':frozen_sha,'inventory_denominator':len(revisions),
            'legacy_scan_sha256':old_sha,'corrected_scan_sha256':new_sha,
            'legacy_scan_rows':len(old),'corrected_scan_rows':len(new) if corrected else None,
            'legacy_png_denominator':len(legacy_png),'corrected_png_denominator':len(corrected_png) if corrected else None,
            'ledger_rows':len(ledger),'missing_paths_in_corrected':len(old.keys()-new.keys()) if corrected else None,
            'additional_paths_in_corrected':len(new.keys()-old.keys()) if corrected else None,
            'counts':dict(counts),
            'identity_boundary':'scan paths and sizes are not proof of mtime/ctime or content revision',
            'ledger':ledger}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('legacy',type=Path)
    parser.add_argument('--corrected',type=Path)
    parser.add_argument('--frozen',type=Path,required=True)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--alias-root',required=True)
    parser.add_argument('--analysis-db',type=Path)
    parser.add_argument('--ledger',type=Path,required=True)
    args = parser.parse_args()
    result = audit(args.legacy,args.corrected,args.frozen,args.manifest_sha256,
                   json.loads(args.config.read_bytes())['sources'],args.alias_root,args.analysis_db)
    with NamedTemporaryFile(dir=args.ledger.parent,mode='w',delete=False) as stream:
        for row in result.pop('ledger'):
            stream.write(json.dumps(row,ensure_ascii=True)+'\n')
        tmp = Path(stream.name)
    tmp.replace(args.ledger)
    result['ledger_sha256'] = sha256(args.ledger.read_bytes()).hexdigest()
    print(json.dumps(result,indent=2,sort_keys=True))


if __name__=='__main__':
    main()
