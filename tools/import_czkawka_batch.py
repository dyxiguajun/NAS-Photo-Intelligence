"""Convert checked, single-anchor Docker exports to nas-photo evidence JSON."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

from nas_photo import organize_evidence as evidence, organize_index as index
from czkawka_docker_batch import checksum, digest as case_digest

SOURCE_ROOT = 'Document/CH_Photos/Photos/DataRestore1/'


def build(batch_path, results, database):
    batch = json.loads(batch_path.read_text())
    info = json.loads((results/'batch-info.json').read_text())
    if info['manifest_sha256']!=checksum(batch_path):
        raise ValueError('Docker checkpoint covers a different batch manifest')
    with index.connect(database) as db:
        indexed = {(row['source_id'],row['relative_path']):row for row in index.files(db)}
    inputs, queries, runs, edges = {},[],[],[]
    for case in batch['cases']:
        if case['kind']!='real_blind':
            continue
        name = case['id']
        result = results/(name+'.json')
        marker = json.loads((results/(name+'.checkpoint.json')).read_text())
        if (marker['output_sha256']!=checksum(result) or marker['bound_samples']!=len(case['files'])
                or marker['signature']!=case_digest({'image':info['image'],
                    'binary_sha256':info['binary_sha256'],'case':case})):
            raise ValueError('Result checksum, runtime or sample count mismatch')
        aliases = {}
        for sample in case['files']:
            source_path = sample['media']
            if not source_path.startswith(SOURCE_ROOT):
                raise ValueError('Unsupported source mapping')
            file = indexed[('restored',source_path.removeprefix(SOURCE_ROOT))]
            revision = evidence.revision(file)
            if list(revision[2:])!=sample['stat'] or file['state']!='present':
                raise ValueError('Source file identity changed after the scan')
            role = 'query' if sample['alias'].startswith('/samples/ref/') else 'reference'
            current = inputs.setdefault(revision,{'revision':list(revision),'inventory_version':file['version'],'roles':[]})
            if role not in current['roles']:
                current['roles'].append(role)
            aliases[sample['alias']]=(revision,role)
        anchor, = (alias for alias,(_,role) in aliases.items() if role=='query')
        query = list(aliases[anchor][0])
        raw = json.loads(result.read_text())
        if len(raw)>1 or (raw and raw[0][0]['path']!=anchor):
            raise ValueError('Czkawka output requires the declared direct anchor')
        candidates = sorted(raw[0][1],key=lambda item:(item['difference'],item['path'])) if raw else []
        queries.append({'revision':query,'status':'matched' if candidates else 'no_candidate_in_scope',
                        'reason':'Czkawka direct report' if candidates else 'No candidate reported for this bound case'})
        runs.append({'query':query,'anchor':anchor,'references':[{'alias':alias,'revision':list(key)}
            for alias,(key,role) in aliases.items() if role=='reference'],'export':raw,
            'export_digest':evidence.digest(raw),'artifact_sha256':marker['output_sha256']})
        for rank,candidate in enumerate(candidates[:10],1):
            ref,role = aliases[candidate['path']]
            if role!='reference':
                raise ValueError('Reported candidate is outside the reference set')
            edges.append({'a':query,'b':list(ref),'relation':'visual_candidate','reason':'Czkawka direct comparison',
                          'rank':rank,'distance':candidate['difference']})
    if not queries:
        raise ValueError('No eligible registered cases')
    records = [inputs[key] for key in sorted(inputs)]
    return {'schema':'organize-evidence-v1','producer':'czkawka_cli','run_id':'czkawka-'+info['manifest_sha256'][:20],
            'scope_description':'Frozen Docker pilot queries against 47 explicitly bound DataRestore1 references',
            'scope_versions':sorted({r['inventory_version'] for r in records}),
            'inputs':records,'inputs_sha256':evidence.digest(records),'top_k':10,
            'expected_counts':{'queries':len(queries),'references':sum('reference' in r['roles'] for r in records),
                               'edges':len(edges)},
            'algorithm':{'implementation':'Czkawka CLI','version':info['engine'],'image':info['image'],
                         'binary_sha256':info['binary_sha256'],'manifest_sha256':info['manifest_sha256'],
                         'metric':'czkawka_reported_distance','profile':'gradient16-lanczos3-dihedral-min1',
                         'max_difference':10},
            'scan_runs':runs,'queries':queries,'edges':edges}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch',required=True,type=Path)
    parser.add_argument('--results',required=True,type=Path)
    parser.add_argument('--database',required=True,type=Path)
    args = parser.parse_args()
    print(index.encode(build(args.batch,args.results,args.database)))


if __name__=='__main__':
    main()
