from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from audit_dsh_png_dimensions import audit


class PngDimensionAuditTest(unittest.TestCase):
    def test_repaired_dimensions_update_coverage_without_promoting_stale_rows(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            def write(name, rows):
                path = root/name
                path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
                return path

            def row(path, width, height, *, size=30):
                return {'p':'/alias/media/'+path,'ext':'png','sz':size,
                        'w':width,'h':height,'ok':width is not None}

            paths = ['tall.png','wide.png','changed.png','missing.png','invalid.png']
            old = write('old.jsonl',[row(path,900,134348800) for path in paths])
            corrected = write('corrected.jsonl',[
                row('tall.png',408,900),row('wide.png',1200,900),
                row('changed.png',1200,900,size=31),row('invalid.png',0,900)])
            manifest = write('inventory.jsonl',[
                {'source':['pictures',path],'state':'present','stat':[30,1,2]} for path in paths])
            digest = sha256(manifest.read_bytes()).hexdigest()
            sources = [{'source_id':'pictures','logical_root':'media'}]
            result = audit(old,corrected,manifest,digest,sources,'/alias')
            self.assertEqual(result['legacy_png_denominator'],5)
            self.assertEqual(result['missing_paths_in_corrected'],1)
            self.assertEqual(result['counts']['left_hires'],1)
            self.assertEqual(result['counts']['corrected_scope_or_size_mismatch'],1)
            self.assertEqual(result['counts']['corrected_dimensions_unavailable'],1)
            self.assertEqual(result['counts']['corrected_dimensions_available'],2)
            self.assertTrue(all(row['execution_allowed'] is False for row in result['ledger']))
            with self.assertRaisesRegex(ValueError,'digest'):
                audit(old,corrected,manifest,'0'*64,sources,'/alias')
