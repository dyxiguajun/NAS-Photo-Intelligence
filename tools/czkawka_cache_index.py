"""Read path coverage from the pinned Czkawka 12.0.2 image cache."""
import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from czkawka_docker_batch import checksum
from czkawka_inventory_batch import eligibility

CACHE_NAME = 'cache_similar_images_16_Gradient_Lanczos3_mirror_flip_rotate90_120.bin'


def records(data, *, raw=False):
    """Bincode 1 default little-endian varints and the pinned ImagesEntry layout."""
    index = 0

    def take(n):
        nonlocal index
        if n < 0 or n > len(data)-index:
            raise ValueError('Truncated Czkawka cache record')
        result = data[index:index+n]
        index += n
        return result

    def integer():
        tag = take(1)[0]
        if tag<251:
            return tag
        if tag not in (251,252,253,254):
            raise ValueError('Invalid Czkawka cache integer')
        return int.from_bytes(take(2**(tag-250)),'little')

    result = {}
    count = integer()
    if count>500_000:
        raise ValueError('Czkawka cache record count exceeds the supported scope')
    for _ in range(count):
        start = index
        path = take(integer()).decode()
        size,width,height,mtime = (integer() for _ in range(4))
        count_hash = integer()
        if not 1<=count_hash<=16:
            raise ValueError('Unexpected Czkawka image hash count')
        for _ in range(count_hash):
            length = integer()
            if not 1<=length<=128:
                raise ValueError('Unexpected Czkawka image hash length')
            take(length)
        integer()  # distance is an engine field, not pair confirmation
        if path in result or not path.startswith('/media/') or not size or not width or not height:
            raise ValueError('Invalid or repeated Czkawka cached file identity')
        result[path] = (data[start:index] if raw else
                        {'size':size,'dimensions':[width,height],'engine_mtime_seconds':mtime})
    if index!=len(data):
        raise ValueError('Trailing data in Czkawka cache')
    return result


def merge_verified(pairs, output):
    """Combine serialized cache records, leaving image matching to Czkawka."""
    combined = {}
    parent = None
    denominator = 0
    proof = []
    seen = set()
    for stage,artifacts in pairs:
        validated = verify(stage,artifacts)
        if parent is not None and parent!=validated['manifest_sha256']:
            raise ValueError('Cannot merge caches from different frozen manifests')
        parent = validated['manifest_sha256']
        denominator += validated['denominator']
        identities = {tuple(row['source']) for row in validated['rows']}
        if seen & identities:
            raise ValueError('Cannot merge overlapping frozen stage identities')
        seen.update(identities)
        data = (artifacts/CACHE_NAME).read_bytes()
        if sha256(data).hexdigest()!=validated['cache_sha256']:
            raise ValueError('Engine cache changed after validation')
        raw = records(data,raw=True)
        combined.update(raw)
        proof.append({'stage_sha256':validated['stage_sha256'],'cache_sha256':validated['cache_sha256']})
    count = len(combined)
    prefix = (bytes([count]) if count<251 else b'\xfb'+count.to_bytes(2,'little') if count<65536
              else b'\xfc'+count.to_bytes(4,'little'))
    content = prefix+b''.join(combined[name] for name in sorted(combined))
    if records(content,raw=True)!=combined:
        raise ValueError('Combined cache failed round-trip verification')
    with NamedTemporaryFile(prefix='.cache-',dir=output.parent) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        os.link(stream.name,output)
    return {'manifest_sha256':parent,'stage_rows':denominator,'cache_rows':count,
            'cache_sha256':checksum(output),'stage_artifacts':proof}


def verify(stage_path, artifacts):
    stage = json.loads(stage_path.read_bytes())
    checkpoint = json.loads((artifacts/'checkpoint.json').read_bytes())
    if (checkpoint['input_stage_sha256']!=checksum(stage_path)
            or checkpoint['parent_manifest_sha256']!=stage['parent_manifest_sha256']
            or checkpoint['output_sha256']!=checksum(artifacts/'candidates.json')):
        raise ValueError('Stage, frozen manifest or candidate checksum mismatch')
    cache = artifacts/CACHE_NAME
    data = cache.read_bytes()
    cached = records(data)
    error = re.compile(r'^Cannot open image file "\u2068(.+?)\u2069":',re.M)
    log = (artifacts/'run.log').read_text()
    failed = set(error.findall(log))
    counts, rows = Counter(), []
    for item in stage['rows']:
        sid,path = item['source']
        alias = f'/media/{sid}/{path}'
        size = item['stat'][0]
        reason = eligibility(path,item['state'],size)
        cached_record = cached.pop(alias,None)
        if cached_record is not None:
            if reason!='engine_input' or cached_record['size']!=size or alias in failed:
                raise ValueError('Cached identity disagrees with the frozen stage')
            state,reason = 'processed','engine_fingerprint_cached'
        elif reason=='engine_input':
            state = 'review'
            reason = 'engine_decode_failed' if alias in failed else 'engine_outcome_unexplained'
        else:
            state = 'review' if reason in ('source_unavailable','file_size_outside_profile') else 'excluded'
        failed.discard(alias)
        counts[reason] += 1
        rows.append({'source':item['source'],'stat':item['stat'],'ledger_state':state,'reason':reason,
                     'engine_record':cached_record,'execution_allowed':False})
    if cached or failed or len(rows)!=checkpoint['denominator']:
        raise ValueError('Unexpected cache, failure or scope count')
    return {'manifest_sha256':stage['parent_manifest_sha256'],'stage_sha256':checksum(stage_path),
            'cache_sha256':sha256(data).hexdigest(),'candidates_sha256':checkpoint['output_sha256'],
            'denominator':len(rows),'counts':dict(counts),'rows':rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',type=Path)
    parser.add_argument('artifacts',type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.stage,args.artifacts),ensure_ascii=True,sort_keys=True))
