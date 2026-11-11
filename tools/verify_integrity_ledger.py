"""Verify a frozen copy-plan denominator and every recorded disposition."""
import argparse
from collections import Counter
from hashlib import file_digest
import json
from pathlib import Path


def digest(path):
    with path.open('rb') as stream:
        return file_digest(stream,'sha256').hexdigest()


def records(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def verify(root):
    summary = json.loads((root/'summary.json').read_bytes())
    for name,expected in summary['outputs_sha256'].items():
        if digest(root/name)!=expected:
            raise ValueError(f'Checksum mismatch: {name}')
    if summary['manifest_sha256']!=digest(root/'frozen-manifest.jsonl'):
        raise ValueError('Frozen denominator checksum mismatch')
    remaining = {}
    for row in records(root/'frozen-manifest.jsonl'):
        key = tuple(row['source'])
        if key in remaining:
            raise ValueError('Duplicate input identity')
        remaining[key] = row
    denominator = len(remaining)
    ready = {}
    states, volumes = Counter(), Counter()
    for name,allowed in (('ready.jsonl',{'processed','review'}),('blocked.jsonl',{'review'}),
                         ('l0-excluded.jsonl',{'excluded'})):
        for row in records(root/name):
            key = tuple(row['source'])
            frozen = remaining.pop(key,None)
            state = row['ledger_state']
            if (frozen is None or row['stat']!=frozen['stat'] or state not in allowed
                    or row['execution_allowed'] is not False):
                raise ValueError('Missing, duplicated or inconsistent ledger disposition')
            states[state] += 1
            volumes[row['status']] += row['stat'][0] or 0
            if name=='ready.jsonl':
                if not row['sha256'] or not row['destination'] or frozen['state']!='present':
                    raise ValueError('Ready row lacks a verified source or target')
                ready[key] = row
            elif name=='l0-excluded.jsonl':
                target = row['replacement']
                kept = ready.get(tuple(target['source']))
                if (kept is None or key==tuple(target['source']) or not row['sha256']
                        or row['sha256']!=target['sha256'] or row['stat'][0]!=target['stat'][0]
                        or kept['sha256']!=target['sha256'] or kept['stat']!=target['stat']
                        or kept['destination']!=target['destination']
                        or row['reason']!='same_size_and_sha256_at_frozen_revision'):
                    raise ValueError('Excluded identity lacks a verified byte-identical retained revision')
    if remaining or states!=summary['ledger_states'] or volumes!=summary['logical_bytes']:
        raise ValueError('Ledger totals do not reconcile with the frozen denominator')
    return {'manifest_sha256':summary['manifest_sha256'],'denominator':denominator,
            'ledger_states':dict(states),'l0_exclusion_replacements_verified':states['excluded'],
            'scope':'frozen planning ledger, no production copies'}


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('preflight',type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.preflight),sort_keys=True))
