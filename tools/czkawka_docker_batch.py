"""Run a manifest of Czkawka scan-only cases on fnOS, with case-level checkpoints."""
from hashlib import file_digest, sha256
from pathlib import Path
import argparse
import fcntl
import json
import os
import re
import subprocess
import time

MEDIA = Path('/vol2/1000')
SCRATCH = Path('/vol3/1001')
PATTERN = re.compile(r'[a-z0-9][a-z0-9_-]{0,55}')
IMAGE = 'jlesage/krokiet@sha256:597a991b9750a44c72fec13ffd7c18f615fdb83757ae5e48a2f6d5c52fdae40a'
ALPINE = 'alpine@sha256:9db2934886712d877c932e07249c75a59e6ca27adfbccc05261987a17bef2d63'
MUSL_SHA256 = 'e90381d8c38479a97f9e98335523208ac285b6e4bcac10f41db791d04a51b14a'
ENTRY = """rc=0
/usr/bin/czkawka_cli "$@" || rc=$?
printf 'cgroup_memory_peak_bytes=' >&2
cat /sys/fs/cgroup/memory.peak >&2 2>/dev/null || true
cat /sys/fs/cgroup/cpu.stat >&2 2>/dev/null || true
exit "$rc"
"""


def encoded(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=True,separators=(',',':'))


def digest(value):
    return sha256(encoded(value).encode()).hexdigest()


def stat_key(path):
    s = path.stat()
    return [s.st_size,s.st_mtime_ns,s.st_ctime_ns]


def checksum(path):
    with path.open('rb') as stream:
        return file_digest(stream,'sha256').hexdigest()


def atomic(path,value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as stream:
        stream.write(encoded(value)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def materialize(root, item):
    alias = item['alias']
    if not re.fullmatch(r'/samples/(?:ref|probe)/[a-z0-9][a-z0-9_.-]{0,90}',alias):
        raise ValueError('Invalid container sample alias')
    if 'media' in item:
        path = MEDIA/item['media']
        if not path.is_relative_to(MEDIA) or path.resolve(strict=True)!=path or not path.is_file():
            raise ValueError('Media path is unavailable or escaped its root')
        if stat_key(path)!=item['stat']:
            raise ValueError(f'Frozen media identity changed: {item["media"]}')
    elif 'embedded' in item:
        parent = materialize(root,item['embedded'])
        path = root/'embedded'/alias.rsplit('/',1)[-1]
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            p = subprocess.run(['exiftool','-b','-ThumbnailImage',str(parent)],capture_output=True,check=True,timeout=25)
            if not 0<len(p.stdout)<=1024**2 or sha256(p.stdout).hexdigest()!=item['sha256']:
                raise ValueError('Embedded preview differs from the verification set')
            if stat_key(parent)!=item['embedded']['stat']:
                raise ValueError('Embedded preview parent changed during extraction')
            path.write_bytes(p.stdout)
            path.chmod(0o444)
        if checksum(path)!=item['sha256']:
            raise ValueError('Staged embedded preview checksum mismatch')
    else:
        path = root/item['fixture']
        if not path.is_relative_to(root) or path.resolve(strict=True)!=path or not path.is_file():
            raise ValueError('Fixture path escaped the batch directory')
        if checksum(path)!=item['sha256']:
            raise ValueError('Synthetic fixture checksum mismatch')
        path.chmod(0o444)
    return path


def run_case(root, work, image, case, uid, gid, binary_mount=()):
    name = case['id']
    if not PATTERN.fullmatch(name):
        raise ValueError('Invalid case ID')
    files = case['files']
    if not 2<=len(files)<=100 or len({item['alias'] for item in files})!=len(files):
        raise ValueError('Each case must contain 2..100 uniquely mounted samples')
    refs = [item['alias'] for item in files if item['alias'].startswith('/samples/ref/')]
    if len(refs)!=1:
        raise ValueError('Exactly one reference per case is required for direct candidate evidence')
    signature = digest({'image':image,'binary_sha256':MUSL_SHA256 if binary_mount else None,'case':case})
    paths = [materialize(root,item) for item in files]
    marker = work/(name+'.checkpoint.json')
    if marker.exists():
        saved = json.loads(marker.read_text())
        output = work/(name+'.json')
        if (saved['signature']!=signature or not output.is_file() or checksum(output)!=saved['output_sha256']):
            raise ValueError(f'Checkpoint or output mismatch: {name}')
        return {'case':name,'status':'checkpoint_reused'}
    before = [stat_key(p) for p in paths]
    if sum(s[0] for s in before)>256*1024**2:
        raise ValueError('Case exceeds 256 MiB declared input limit')
    container = 'nasphoto-' + sha256(str(root).encode()).hexdigest()[:10] + '-' + name
    partial = work/(name+'.partial.json')
    if partial.exists():
        partial.unlink()
    cmd = ['docker','run','--rm','--name',container,'--pull','never','--network','none',
           '--read-only','--user',f'{uid}:{gid}','--cap-drop','ALL','--security-opt','no-new-privileges',
           '--cpus','1','--memory','768m','--memory-swap','768m','--pids-limit','64',
           '--env','CZKAWKA_CACHE_PATH=/work/cache','--env','XDG_CACHE_HOME=/work/cache','--env','HOME=/work',
           '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=64m',
           '--mount',f'type=bind,source={work},target=/work',*binary_mount]
    for item,path in zip(files,paths):
        if ',' in str(path):
            raise ValueError('Comma in bind path is not supported by this batch wrapper')
        cmd+=['--mount',f'type=bind,source={path},target={item["alias"]},readonly']
    options = ['image','-d','/samples/probe','-r','/samples/ref','-T','1','-W','-N',
               '-p',f'/work/{partial.name}']
    if case.get('profile','tuned')=='tuned':
        options+=['-g','Gradient','-c','16','-z','Lanczos3','-s','10',
                  '--geometric-invariance','mirror-flip-rotate90']
    elif case['profile']!='default':
        raise ValueError('Unknown Czkawka scan profile')
    if case.get('minimum_bytes') is not None:
        if case['minimum_bytes'] not in (1,16384):
            raise ValueError('Unsupported experiment minimum file size')
        options+=['-m',str(case['minimum_bytes'])]
    if case.get('cold'):
        options+=['-H']
    cmd+=['--entrypoint','/bin/sh',image,'-c',ENTRY,'scan-only',*options]
    start = time.monotonic()
    try:
        p = subprocess.run(cmd,capture_output=True,text=True,timeout=65)
    except subprocess.TimeoutExpired:
        subprocess.run(['docker','rm','-f',container],capture_output=True,timeout=15)
        raise
    elapsed = time.monotonic()-start
    log = work/(name+'.log')
    log.write_text(p.stdout+p.stderr)
    if p.returncode:
        raise RuntimeError(f'Docker scan {name} failed with {p.returncode}; see {log}')
    if before!=[stat_key(path) for path in paths]:
        raise ValueError('A source changed during the scan')
    groups = json.loads(partial.read_text())
    aliases = {item['alias'] for item in files}
    for group in groups:
        if group[0]['path']!=refs[0] or any(x['path'] not in aliases-{refs[0]} for x in group[1]):
            raise ValueError('Export contains an undeclared sample')
    output = work/(name+'.json')
    partial.replace(output)
    peak = re.search(r'cgroup_memory_peak_bytes=(\d+)',p.stderr)
    cpu = re.search(r'^usage_usec (\d+)',p.stderr,re.M)
    result = {'case':name,'signature':signature,'image':image,'arguments':options,
              'wall_seconds':elapsed,'memory_peak_bytes':int(peak[1]) if peak else None,
              'cpu_usage_usec':int(cpu[1]) if cpu else None,'group_count':len(groups),
              'candidate_count':sum(len(group[1]) for group in groups),
              'bound_samples':len(files),'output_sha256':checksum(output)}
    atomic(marker,result)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('workspace',type=Path)
    parser.add_argument('--upstream-musl',action='store_true')
    args=parser.parse_args()
    root=args.workspace.resolve(strict=True)
    if root.parent!=SCRATCH or not root.name.startswith('nas-photo-czkawka.'):
        raise ValueError('Expected a dedicated /vol3/1001/nas-photo-czkawka.* workspace')
    work=root/'results'
    work.mkdir(exist_ok=True)
    uid,gid=1000,1001
    os.chown(work,uid,gid)
    work.chmod(0o2770)
    cached=work/'cache'
    cached.mkdir(exist_ok=True)
    os.chown(cached,uid,gid)
    cached.chmod(0o2770)
    plan=json.loads((root/'batch.json').read_text())
    cases=plan['cases']
    if not 1<=len(cases)<=80 or len({case['id'] for case in cases})!=len(cases):
        raise ValueError('Invalid batch case count or duplicate case identity')
    image,binary_mount = IMAGE,[]
    if args.upstream_musl:
        binary = root/'czkawka_cli'
        if binary.is_symlink() or checksum(binary)!=MUSL_SHA256:
            raise ValueError('Upstream musl binary checksum mismatch')
        binary.chmod(0o555)
        image = ALPINE
        binary_mount = ['--mount',f'type=bind,source={binary},target=/usr/bin/czkawka_cli,readonly']
    version=subprocess.check_output(['docker','run','--rm','--pull','never','--network','none',
                    '--read-only',*binary_mount,'--entrypoint','/usr/bin/czkawka_cli',image,'--version'],
                    text=True,timeout=30)
    with (root/'batch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        atomic(work/'batch-info.json',{'manifest_sha256':checksum(root/'batch.json'),'engine':version,'image':image,
                                       'binary_sha256':MUSL_SHA256 if binary_mount else None})
        for case in cases:
            print(encoded(run_case(root,work,image,case,uid,gid,binary_mount)),flush=True)


if __name__=='__main__':
    main()
