import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def stop_group(p):
    # Only a process group created by this function; never generic kill/port cleanup.
    for sig in [signal.SIGTERM, signal.SIGKILL]:
        try: os.killpg(p.pid, sig)
        except ProcessLookupError: pass
        except PermissionError:
            # macOS can retain a protected/stale process group after our leader was reaped.
            # Never follow it to unrelated processes. A still-live owned leader is an error.
            if p.returncode is not None: return
            raise
        try: p.wait(timeout=1)
        except subprocess.TimeoutExpired: pass


def run_worker(request, timeout):
    request = {**request, "supervisor_pid": os.getpid()}
    env = dict(os.environ)
    if request.get("operation") == "visual":
        env.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"})
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.Popen([sys.executable, "-m", "nas_photo.worker"], stdin=subprocess.PIPE,
                             stdout=out, stderr=err, env=env, start_new_session=True)
        try:
            if request.get("operation") != "visual":
                p.communicate(json.dumps(request).encode(), timeout=timeout)
            else:
                deadline = time.monotonic() + timeout
                data = json.dumps(request).encode()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise subprocess.TimeoutExpired(p.args, timeout)
                    try:
                        p.communicate(data, timeout=min(0.2, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        data = None
                        # Inspect only this worker PID. No generic process cleanup.
                        usage = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(p.pid)],
                                               capture_output=True, text=True, timeout=1)
                        if usage.stdout.strip() and int(usage.stdout.strip()) > request["max_rss_mb"]*1024:
                            stop_group(p)
                            return {"ok": False, "error": {"code": "memory_limit", "message": "isolated decoder exceeded measured RSS ceiling"}}
        except subprocess.TimeoutExpired:
            stop_group(p)
            return {"ok": False, "error": {"code": "timeout", "message": "bounded worker deadline exceeded"}}
        except BaseException:
            stop_group(p)
            raise
        if out.tell() > 4 * 1024 * 1024:
            return {"ok": False, "error": {"code": "output_limit", "message": "worker response too large"}}
        out.seek(0)
        try:
            result = json.loads(out.read())
            if p.returncode != 0: raise ValueError("worker exited abnormally")
            return result
        except (ValueError, TypeError):
            return {"ok": False, "error": {"code": "worker_crash", "message": "worker failed without a valid result"}}
