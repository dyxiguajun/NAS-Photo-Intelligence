"""Stage directory-level Czkawka scans against a frozen inventory, without media writes."""
import argparse
from collections import Counter, defaultdict
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from tempfile import TemporaryDirectory
import time

from czkawka_docker_batch import ALPINE, ENTRY, MUSL_SHA256, atomic, checksum, digest

EXTENSIONS = ('jpg','jpeg','png','gif','bmp','tiff','tif','webp')
MAX_FILE = 32*1024**2
FLAT_RESTORE = {'JPEG Images','PNG Images'}


def split_hex(items, prefix=''):
    if len(items)<=4000:
        yield prefix,items
        return
    groups = defaultdict(list)
    for item in items:
        name = item['source'][1].split('/',1)[1]
        if not re.fullmatch(r'[0-9A-F]{8}\.(?:jpg|png)',name,re.I) or name.split('.')[0]!=name.split('.')[0].upper():
            raise ValueError('Prefix sharding requires the frozen uppercase recovery naming convention')
        groups[name[len(prefix)]].append(item)
    for key,rows in sorted(groups.items()):
        yield from split_hex(rows,prefix+key)


def relative(path):
    value = PurePosixPath(path)
    if value.is_absolute() or not value.parts or '..' in value.parts or str(value)!=path:
        raise ValueError('Invalid inventory or bind path')
    return value


def eligibility(path, state, size):
    if state!='present':
        return 'source_unavailable'
    if not size:
        return 'zero_byte'
    if PurePosixPath(path).suffix.lower().lstrip('.') not in EXTENSIONS:
        return 'format_outside_profile'
    if size>MAX_FILE:
        return 'file_size_outside_profile'
    return 'engine_input'


def partition(manifest, config, output):
    if output.exists():
        raise FileExistsError('Inventory plan already exists')
    output.parent.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(prefix='.inventory-plan-',dir=output.parent) as tmp:
        stage = Path(tmp)/'plan'
        summary = build_partition(manifest,config,stage)
        verify_partition(manifest,stage)
        if output.exists():
            raise FileExistsError('Inventory plan appeared during partition')
        stage.rename(output)
    return summary


def build_partition(manifest, config, output):
    """Assign all inventory rows to staged engine inputs or explicit exceptions."""
    scopes = {s['source_id']:s['logical_root'] for s in json.loads(config.read_bytes())['sources']}
    input_digest = checksum(manifest)
    rows, counts = defaultdict(list), Counter()
    for line in manifest.read_bytes().splitlines():
        row = json.loads(line)
        source,path = row['source']
        part = relative(path)
        folder = part.parts[0] if source=='restored' else ''
        counts[eligibility(path,row['state'],row['stat'][0])] += 1
        rows[(source,folder)].append(row)
    if checksum(manifest)!=input_digest:
        raise ValueError('Frozen manifest changed during partition')
    output.mkdir()
    stages = []
    for (source,folder),items in sorted(rows.items()):
        batches = split_hex(items) if source=='restored' and folder in FLAT_RESTORE else [('',items)]
        for prefix,batch in batches:
            stage = {'schema':'czkawka-inventory-stage-v1','parent_manifest_sha256':input_digest,
                     'stage':f'{len(stages)+1:03}-{source}','source_id':source,
                     'logical_root':str(relative(scopes[source])),'folder':folder,'prefix':prefix,'rows':batch}
            name = stage['stage']+'.json'
            atomic(output/name,stage)
            stages.append({'path':name,'sha256':checksum(output/name),'input_rows':len(batch),'prefix':prefix,
                           'engine_inputs':sum(eligibility(r['source'][1],r['state'],r['stat'][0])=='engine_input'
                                               for r in batch)})
    summary = {'manifest_sha256':input_digest,'denominator':sum(counts.values()),'counts':dict(counts),
               'stages':stages,'profile':{'extensions':list(EXTENSIONS),'min_bytes':1,'max_bytes':MAX_FILE},
               'global_cross_stage_search_completed':False}
    atomic(output/'plan.json',summary)
    return summary


def verify_partition(manifest, root):
    plan = json.loads((root/'plan.json').read_bytes())
    scope = checksum(manifest)
    if plan['manifest_sha256']!=scope:
        raise ValueError('Inventory plan refers to another frozen manifest')
    rows = {}
    for line in manifest.read_bytes().splitlines():
        item = json.loads(line)
        identity = tuple(item['source'])
        if identity in rows:
            raise ValueError('Duplicate frozen file identity')
        rows[identity] = item
    denominator = len(rows)
    counts = Counter()
    for entry in plan['stages']:
        path = root/entry['path']
        if checksum(path)!=entry['sha256']:
            raise ValueError('Inventory shard checksum mismatch')
        stage = json.loads(path.read_bytes())
        if stage['parent_manifest_sha256']!=scope or len(stage['rows'])!=entry['input_rows']:
            raise ValueError('Inventory shard scope or count mismatch')
        for item in stage['rows']:
            key = tuple(item['source'])
            if rows.pop(key,None)!=item:
                raise ValueError('Unexpected, repeated or mismatched inventory shard row')
            counts[eligibility(key[1],item['state'],item['stat'][0])] += 1
    if rows or denominator!=plan['denominator'] or counts!=plan['counts']:
        raise ValueError('Inventory stages do not cover the frozen denominator')
    return {'manifest_sha256':scope,'denominator':denominator,'stages':len(plan['stages']),'counts':dict(counts)}


def verify_revisions(base, stage):
    root = base/stage['logical_root']
    if root.resolve(strict=True)!=root or not root.is_dir():
        raise ValueError('Source root is unavailable or symlinked')
    actual = {}
    folder = stage['folder']
    scope = root/folder if folder else root
    if scope.resolve(strict=True)!=scope:
        raise ValueError('Stage folder is not a real directory')
    for directory, dirs, files in os.walk(scope,followlinks=False,onerror=lambda err: (_ for _ in ()).throw(err)):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if not (parent/name).is_symlink()]
        prefix = stage.get('prefix','')
        if prefix and dirs:
            raise ValueError('Recovery shard requires a flat directory')
        for filename in files:
            if prefix:
                if not re.fullmatch(r'[0-9A-F]{8}\.(?:jpg|png)',filename,re.I) or filename.split('.')[0]!=filename.split('.')[0].upper():
                    raise ValueError('Recovery filename no longer fits the frozen sharding convention')
                if not filename.startswith(prefix):
                    continue
            path = parent/filename
            if path.is_symlink() or not path.is_file():
                continue
            st = path.stat(follow_symlinks=False)
            actual[path.relative_to(root).as_posix()] = [st.st_size,st.st_mtime_ns,st.st_ctime_ns]
    expected = {r['source'][1]:r['stat'] for r in stage['rows'] if r['state']=='present'}
    if actual!=expected:
        raise ValueError(f'Stage revision drift: {len(actual.keys()-expected.keys())} new, '
                         f'{len(expected.keys()-actual.keys())} missing, '
                         f'{sum(actual[p]!=expected[p] for p in actual.keys() & expected.keys())} changed')
    return root


def run(args):
    path = args.stage.resolve(strict=True)
    workspace = path.parent
    stage = json.loads(path.read_bytes())
    if (stage['schema']!='czkawka-inventory-stage-v1' or not re.fullmatch('[a-z0-9_-]{1,55}',stage['stage'])
            or not re.fullmatch('[a-f0-9]{64}',stage['parent_manifest_sha256'])):
        raise ValueError('Invalid stage manifest')
    relative(stage['logical_root'])
    if ',' in stage['logical_root']:
        raise ValueError('Comma in a Docker source root is unsupported')
    if stage['folder']:
        relative(stage['folder'])
    media = args.media.resolve(strict=True)
    state_volume = args.state_volume.resolve(strict=True)
    if (media in workspace.parents or workspace==media or not state_volume.is_mount()
            or state_volume not in workspace.parents):
        raise ValueError('Expected a mounted independent analysis workspace')
    binary = args.binary.resolve(strict=True)
    if checksum(binary)!=MUSL_SHA256:
        raise ValueError('Unexpected Czkawka binary')
    signature = digest({'stage_sha256':checksum(path),'image':ALPINE,'binary':MUSL_SHA256,
                        'extensions':EXTENSIONS,'max_bytes':MAX_FILE,'profile':'gradient16-lanczos3-dihedral-min1'})
    status = workspace/'checkpoint.json'
    output = workspace/'candidates.json'
    marker_lock = workspace.parent/f'.inventory-{signature[:24]}.lock'
    with marker_lock.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        root = verify_revisions(media,stage)
        if status.exists():
            result = json.loads(status.read_bytes())
            if result['signature']!=signature or checksum(output)!=result['output_sha256']:
                raise ValueError('Czkawka stage checkpoint does not reconcile')
            return {**result,'reused':True}
        cache = workspace/'cache'
        cache.mkdir(exist_ok=True)
        # Runtime UID writes only the isolated output area and cache.
        result_dir = workspace/'engine'
        result_dir.mkdir(exist_ok=True)
        uid,gid = map(int,args.user.split(':'))
        if uid<=0 or gid<=0:
            raise ValueError('Engine runtime must use an unprivileged uid:gid')
        for folder in (cache,result_dir):
            os.chown(folder,uid,gid)
            folder.chmod(0o770)
        partial = result_dir/'partial.json'
        partial.unlink(missing_ok=True)
        alias = '/media/'+stage['source_id']
        scan = alias+('/'+stage['folder'] if stage['folder'] else '')
        container = 'nas-photo-inventory-'+signature[:20]
        options = ['image','-d',scan,'-x',','.join(EXTENSIONS),'-m','1','-i',str(MAX_FILE),'-L','-T','1','-W','-N',
                   '-D','NONE','-g','Gradient','-c','16','-z','Lanczos3','-s','10',
                   '--geometric-invariance','mirror-flip-rotate90','-C','/engine/partial.json']
        if prefix := stage.get('prefix'):
            if not re.fullmatch('[A-F0-9]{1,8}',prefix):
                raise ValueError('Invalid recovery shard prefix')
            for index,wanted in enumerate(prefix):
                for other in '0123456789ABCDEF':
                    if other!=wanted:
                        options.extend(['-E',scan+'/'+prefix[:index]+other+'*'])
        command = ['docker','run','--rm','--name',container,'--pull','never','--network','none',
                   '--read-only','--user',args.user,'--cap-drop','ALL','--security-opt','no-new-privileges:true',
                   '--cpus','0.65','--memory','768m','--memory-swap','768m','--pids-limit','64',
                   '--env','CZKAWKA_CACHE_PATH=/cache','--env','XDG_CACHE_HOME=/cache','--env','HOME=/tmp',
                   '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=32m',
                   '--mount',f'type=bind,src={root},dst={alias},readonly,bind-propagation=rprivate,bind-recursive=readonly',
                   '--mount',f'type=bind,src={cache},dst=/cache',
                   '--mount',f'type=bind,src={result_dir},dst=/engine',
                   '--mount',f'type=bind,src={binary},dst=/usr/bin/czkawka_cli,readonly',
                   '--entrypoint','/bin/sh',ALPINE,'-c',ENTRY,'scan-only',*options]
        started = time.monotonic()
        with (workspace/'run.log').open('a') as log:
            try:
                subprocess.run(command,stdout=log,stderr=log,check=True,timeout=args.seconds)
            except subprocess.TimeoutExpired:
                subprocess.run(['docker','rm','-f',container],capture_output=True,timeout=15)
                raise
        verify_revisions(media,stage)
        groups = json.loads(partial.read_bytes())
        selected = {alias+'/'+r['source'][1] for r in stage['rows']
                    if eligibility(r['source'][1],r['state'],r['stat'][0])=='engine_input'}
        for group in groups:
            if any(item['path'] not in selected for item in group):
                raise ValueError('Candidate export refers to an undeclared engine input')
        with partial.open('rb') as saved:
            os.fsync(saved.fileno())
        partial.replace(output)
        log_text = (workspace/'run.log').read_text(errors='replace')
        cache_counts = re.findall(r'Properly saved to file [^\d]*(\d+)[^\d]*cache entries',log_text)
        memory_peaks = re.findall(r'cgroup_memory_peak_bytes=(\d+)',log_text)
        result = {'signature':signature,'parent_manifest_sha256':stage['parent_manifest_sha256'],
                  'input_stage_sha256':checksum(path),'output_sha256':checksum(output),
                  'denominator':len(stage['rows']),'engine_inputs':len(selected),
                  'excluded_by_profile':len(stage['rows'])-len(selected),
                  'groups':len(groups),'wall_seconds':round(time.monotonic()-started,3),
                  'engine_reported_cache_entries':int(cache_counts[-1]) if cache_counts else None,
                  'memory_peak_bytes':int(memory_peaks[-1]) if memory_peaks else None,
                  'independent_decode_coverage_verified':False,'direct_pair_confirmation':False,'reused':False}
        atomic(status,result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action',required=True)
    prepare = sub.add_parser('plan')
    prepare.add_argument('manifest',type=Path)
    prepare.add_argument('config',type=Path)
    prepare.add_argument('output',type=Path)
    verify = sub.add_parser('verify')
    verify.add_argument('manifest',type=Path)
    verify.add_argument('output',type=Path)
    runner = sub.add_parser('run')
    runner.add_argument('stage',type=Path)
    runner.add_argument('--media',type=Path,required=True)
    runner.add_argument('--state-volume',type=Path,required=True)
    runner.add_argument('--binary',type=Path,required=True)
    runner.add_argument('--user',required=True)
    runner.add_argument('--seconds',type=int,default=900)
    args = parser.parse_args()
    if args.action=='plan':
        result = partition(args.manifest,args.config,args.output)
    elif args.action=='verify':
        result = verify_partition(args.manifest,args.output)
    else:
        result = run(args)
    print(json.dumps(result,sort_keys=True))


if __name__ == '__main__':
    main()
