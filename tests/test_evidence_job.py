from argparse import Namespace
from hashlib import sha256
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import time

from PIL import Image

from nas_photo.organize_index import apply_batch, connect, register_sources

PROJECT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = PROJECT/'tools/nas-evidence'
sys.path.insert(0,str(EVIDENCE_ROOT))
from nas_evidence_worker import PROFILE, canonical, digest, identity, run, validate_result
from evidence_intake import READER, WORKER, import_results, plan


class EvidenceJobTest(unittest.TestCase):
    def test_sigkill_during_second_chunk_resumes_to_identical_checkpoints(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            media = base/'media'
            source = media/'source'
            source.mkdir(parents=True)
            rows = []
            for i in range(129):
                path = source/f'{i:03}.jpg'
                path.write_bytes(f'fixture {i:03}'.encode())
                rows.append({'path':path.name,'stat':list(identity(path.stat())),'ops':['metadata','sha256']})
            exiftool = base/'exiftool'
            exiftool.write_text(f'#!{sys.executable}\n'+"""
import json, os, sys, time
from pathlib import Path
if sys.argv[1:] == ['-ver']:
    print('test-1')
    sys.exit()
names = [arg for arg in sys.argv if arg.startswith('./')]
if 'PAUSE_MARKER' in os.environ and names == ['./128.jpg']:
    Path(os.environ['PAUSE_MARKER']).touch()
    time.sleep(30)
print(json.dumps([{'SourceFile':name,'File:ImageWidth':1,'File:ImageHeight':1} for name in names]))
""")
            exiftool.chmod(0o700)
            manifest = {'profile':PROFILE,'source_id':'fixture','device_id':'local','logical_root':'source',
                        'runner_sha256':digest(WORKER),'reader_sha256':digest(READER),'exiftool_version':'test-1',
                        'rows':rows}
            payload = canonical(manifest)
            job_id = sha256(payload).hexdigest()

            def command(name):
                stage = base/name/job_id
                stage.mkdir(parents=True)
                path = stage/'manifest.json'
                path.write_bytes(payload)
                return stage,[sys.executable,'-B',str(WORKER),'run','--manifest',str(path),
                              '--media-root',str(media),'--exiftool',str(exiftool),'--seconds','30']

            baseline, normal = command('baseline')
            subprocess.run(normal,check=True,capture_output=True,timeout=10)
            resumed, interrupted = command('interrupted')
            marker = base/'waiting'
            proc = subprocess.Popen(interrupted,start_new_session=True,stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,env={**os.environ,'PAUSE_MARKER':str(marker)})
            try:
                until = time.monotonic()+10
                while not marker.exists() and proc.poll() is None and time.monotonic()<until:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                first = resumed/'00001.json'
                self.assertTrue(first.exists())
                self.assertFalse((resumed/'00002.json').exists())
                first_revision = first.stat().st_mtime_ns
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid,signal.SIGKILL)
                proc.wait(timeout=5)
            self.assertEqual(proc.returncode,-signal.SIGKILL)
            subprocess.run(interrupted,check=True,capture_output=True,timeout=10)
            self.assertEqual(first.stat().st_mtime_ns,first_revision)
            self.assertEqual([p.read_bytes() for p in sorted(baseline.glob('000*.json'))],
                             [p.read_bytes() for p in sorted(resumed.glob('000*.json'))])

    def test_resume_and_current_revision_import(self):
        exiftool = PROJECT/'tools/exiftool/exiftool'
        if not exiftool.is_file():
            system_exiftool = shutil.which('exiftool')
            if system_exiftool is None:
                self.skipTest('Local ExifTool unavailable')
            exiftool = Path(system_exiftool)
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            media = base/'media'
            src = media/'source'
            src.mkdir(parents=True)
            names = ['-test.jpg','modified.png','missing.jpg']
            for name in names:
                Image.new('RGB',(11,7),(25,50,75)).save(src/name)
            scope = {'source_id':'source','device_id':'test','logical_root':'source'}
            with connect(base/'audit.sqlite3',write=True) as db:
                register_sources(db,[scope])
                apply_batch(db,'source','base',[{'path':name,'stat':list(identity((src/name).stat()))}
                                                for name in names])
                version = subprocess.check_output([str(exiftool),'-ver'],text=True).strip()
                manifest = plan(db,'source',3,1024**2,version,all_sha=True)
                self.assertEqual(manifest['profile'],PROFILE)
                self.assertEqual(manifest['runner_sha256'],digest(WORKER))
                self.assertEqual(manifest['reader_sha256'],digest(READER))
                job_id = sha256(canonical(manifest)).hexdigest()
                path = base/'jobs'/job_id/'manifest.json'
                path.parent.mkdir(parents=True)
                path.write_bytes(canonical(manifest))
                args = Namespace(manifest=path,media_root=media,exiftool=str(exiftool),seconds=30,yield_busy=False)
                (src/'missing.jpg').unlink()
                run(args)
                batch = path.parent/'00001.json'
                checkpoint_identity = batch.stat().st_mtime_ns
                run(args)
                self.assertEqual(batch.stat().st_mtime_ns,checkpoint_identity)
                result = json.loads(batch.read_bytes())
                by_name = {r['path']:r for r in result['records']}
                self.assertEqual(by_name['-test.jpg']['sha256'],digest(src/'-test.jpg'))
                self.assertIn('identity',by_name['missing.jpg']['errors'])
                wrong = {**result,'records':result['records'][:-1]}
                with self.assertRaisesRegex(ValueError,'Incomplete'):
                    validate_result(wrong,manifest['rows'],job_id,1)
                apply_batch(db,'source','new',[{'path':'modified.png','stat':[700,100,100]}])
                report = import_results(db,path,manifest,job_id)
                self.assertEqual(report['counts']['identity_read_error'],1)
                self.assertEqual(report['commits'][0]['stale_paths'],['modified.png'])
                replay = import_results(db,path,manifest,job_id)
                self.assertTrue(replay['commits'][0]['reused'])
                self.assertEqual(replay['commits'][0]['stale_paths'],['modified.png'])
                verified = db.execute("SELECT evidence FROM organize_files WHERE relative_path='-test.jpg'").fetchone()
                observed = json.loads(verified[0])
                self.assertEqual((observed['width'],observed['height']),(11,7))
                self.assertEqual(observed['sha256'],digest(src/'-test.jpg'))
                self.assertEqual(db.execute("SELECT size,evidence FROM organize_files WHERE relative_path='modified.png'").fetchone()[:],
                                 (700,'{}'))
