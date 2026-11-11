from argparse import Namespace
from hashlib import sha256
from pathlib import Path
import json
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from nas_photo.organize_index import apply_batch, connect, register_sources

PROJECT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = PROJECT/'tools/nas-evidence'
sys.path.insert(0,str(EVIDENCE_ROOT))
from nas_evidence_worker import PROFILE, canonical, digest, identity, run, validate_result
from evidence_intake import READER, WORKER, import_results, plan


class EvidenceJobTest(unittest.TestCase):
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
