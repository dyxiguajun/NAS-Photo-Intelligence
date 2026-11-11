"""Resume one immutable evidence job at container start, then report its result."""
import argparse
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest',type=Path)
    args = parser.parse_args()
    root = args.manifest.parent
    sys.path.insert(0,str(root))
    from nas_evidence_worker import chunks, job, validate_result, write_checkpoint

    manifest, job_id = job(args.manifest)
    completed, failed = root/'complete.json', root/'failed.json'
    batches = list(chunks(manifest['rows']))
    if not (completed.exists() or failed.exists()):
        try:
            with (root/'run.log').open('a') as log:
                print(f'Container starting {job_id}',file=log,flush=True)
                # Older pinned runners lack the large-file profile.
                large = manifest['profile']=='native-sha256-large-file-v1'
                while True:
                    subprocess.run(
                        [sys.executable,'-B',str(root/'nas_evidence_worker.py'),'run','--manifest',str(args.manifest),
                         '--media-root','/media','--seconds','900'],
                        stdout=log,stderr=log,check=True,timeout=1850 if large else 1000)
                    if (root/f'{len(batches):05}.json').is_file():
                        break
                import json
                for number,batch in enumerate(batches,1):
                    path = root/f'{number:05}.json'
                    validate_result(json.loads(path.read_bytes()),batch,job_id,number)
                write_checkpoint(completed,{'manifest_sha256':job_id,'chunks':len(batches),'rows':len(manifest['rows'])})
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            try:
                write_checkpoint(failed,{'manifest_sha256':job_id,'error':str(error)})
            except OSError as marker_error:
                print(f'Unable to persist job failure for {job_id}: {marker_error}',file=sys.stderr,flush=True)
    if completed.exists():
        print(f'Completed {job_id}',flush=True)
    else:
        print(f'Stopped on failure {job_id}',flush=True)
    # Docker owns boot recovery. Keep a completed or failed container idle
    # until it is stopped explicitly, preventing a restart loop.
    while True:
        time.sleep(60)


if __name__ == '__main__':
    main()
