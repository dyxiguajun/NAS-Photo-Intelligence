"""Local-only fault fixture. Real supervisor, ps samples, signals and OS processes.

The spy delegates every system call unchanged. It only logs and releases the
fixture after the supervisor has sampled that exact worker at least once.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from unittest.mock import patch
from nas_photo import process

INJECT = r'''
import sys
if '-m' in sys.orig_argv and 'nas_photo.worker' in sys.orig_argv:
    import json, os, resource, signal, time
    from pathlib import Path
    import nas_photo.imaging as imaging
    def controlled(request):
        base = Path(os.environ['NAS_PHOTO_PROBE_DIR'])
        mode = os.environ['NAS_PHOTO_PROBE_MODE']
        def mark(name, **values):
            payload = dict(pid=os.getpid(), pgid=os.getpgrp(), monotonic=time.monotonic(), **values)
            temporary = base / (name + '.tmp')
            temporary.write_text(json.dumps(payload))
            temporary.replace(base / name)
        def wait_for(name):
            deadline = time.monotonic() + 8
            while not (base / name).exists():
                if time.monotonic() >= deadline: raise RuntimeError('fixture handshake deadline: ' + name)
                time.sleep(.01)
        if mode == 'ignore_term': signal.signal(signal.SIGTERM, signal.SIG_IGN)
        mark('ready')
        wait_for('allocate')
        if mode == 'crash': os._exit(7)
        size = int(os.environ['NAS_PHOTO_PROBE_MIB']) * 1024**2
        mark('allocating', requested_bytes=size)
        # Random-filled, actually accessed pages; retain and revisit them until
        # the parent releases this fixture or the real supervisor terminates it.
        data = os.urandom(size)
        mark('allocated', allocated_bytes=len(data), worker_highwater_at_allocation=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        deadline = time.monotonic() + 8
        while not (base / 'release').exists():
            if time.monotonic() >= deadline: raise RuntimeError('fixture release deadline')
            checksum = sum(data[::4096])
            time.sleep(.01)
        return {'fixture_completed': True, 'allocated_bytes': len(data), 'pid': os.getpid()}
    imaging.analyze_file = controlled
'''


def controlled_worker(request=None, *, mode='rss', allocation_mib=200, timeout=10):
    request = {**(request or {}), 'operation': 'visual', 'max_rss_mb': 128}
    events, workers, samples, signals = [], [], [], []
    actual_run, actual_popen, actual_killpg = subprocess.run, subprocess.Popen, os.killpg
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='nas-worker-probe-') as directory:
        base = Path(directory)
        (base / 'sitecustomize.py').write_text(INJECT)
        def spawn(*args, **kwargs):
            p = actual_popen(*args, **kwargs)
            if args and isinstance(args[0], list) and '-m' in args[0] and 'nas_photo.worker' in args[0]:
                workers.append(p); events.append({'event': 'spawn', 'pid': p.pid, 'monotonic': time.monotonic()})
            return p
        def sampled_run(*args, **kwargs):
            before = time.monotonic()
            r = actual_run(*args, **kwargs)
            command = args[0] if args else []
            if command[:3] == ['/bin/ps', '-o', 'rss=']:
                pid = int(command[-1]); rss = int(r.stdout.strip()) if r.stdout.strip() else None
                samples.append({'pid': pid, 'monotonic': before, 'duration': time.monotonic()-before,
                                'rss_KiB': rss, 'ps_exit_code': r.returncode})
                if (base / 'ready').exists():
                    ready = json.loads((base / 'ready').read_text())
                    if ready['pid'] == pid and rss is not None:
                        if not (base / 'allocate').exists():
                            (base / 'allocate').touch()
                            events.append({'event': 'monitor_observed_then_allocate', 'pid': pid, 'monotonic': time.monotonic()})
                        elif mode == 'normal' and (base / 'allocated').exists():
                            (base / 'release').touch()
            return r
        def killpg(pgid, sig):
            signals.append({'pgid': pgid, 'signal': int(sig), 'monotonic': time.monotonic()})
            return actual_killpg(pgid, sig)
        env = {'PYTHONPATH': directory, 'NAS_PHOTO_PROBE_DIR': directory,
               'NAS_PHOTO_PROBE_MODE': mode, 'NAS_PHOTO_PROBE_MIB': str(allocation_mib)}
        try:
            with patch.dict(os.environ, env), patch('subprocess.Popen', side_effect=spawn), \
                 patch('subprocess.run', side_effect=sampled_run), patch('os.killpg', side_effect=killpg):
                response = process.run_worker(request, timeout)
            markers = {name: json.loads((base/name).read_text()) for name in ('ready','allocating','allocated') if (base/name).exists()}
            return response, {'mode': mode, 'elapsed_seconds': time.monotonic()-start, 'limit_RSS_KiB': 128*1024,
                              'allocation_logical_MiB': allocation_mib, 'events': events, 'samples': samples,
                              'sampled_max_RSS_KiB': max((s['rss_KiB'] or 0 for s in samples), default=0),
                              'signals': signals, 'markers': markers,
                              'workers': [{'pid':p.pid,'returncode':p.returncode,'reaped':p.returncode is not None} for p in workers]}
        finally:
            for p in workers:
                if p.poll() is None: process.stop_group(p)
