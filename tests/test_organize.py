import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from nas_photo import organize, organize_decisions as decisions, organize_index as index
from nas_photo import organize_evidence as evidence
from nas_photo.organize_exercise import checksum, exercise, verified_copy
from nas_photo.organize_reconcile import reconcile
from nas_photo.organize_sha import exact_matches

DIGEST = 'a' * 64


def observation(path='asset.jpg', size=20, mtime=1, **evidence):
    return {'path':path,'stat':[size,mtime,2],'evidence':evidence}


def item(path, *, source='restored', size=20, decode='valid', width=180, height=120, **evidence):
    visual = evidence.pop('visual', {'status':decode,'width':width,'height':height})
    return {'source_id':source,'relative_path':path,'size':size,'mtime_ns':1,'ctime_ns':2,
            'state':'present','version':'fixture','evidence':{'visual':visual,**evidence}}


class InventoryTest(unittest.TestCase):
    def test_object_sha_lookup_uses_frozen_revision_and_does_not_export_paths(self):
        with tempfile.TemporaryDirectory() as tmp, index.connect(Path(tmp)/'audit.sqlite3',write=True) as db:
            index.register_sources(db,[{'source_id':'one','device_id':'test','logical_root':'first'}])
            index.apply_batch(db,'one','v1',[observation(sha256=DIGEST)])
            frozen = Path(tmp)/'frozen.jsonl'
            frozen.write_text(json.dumps({'source':['one','asset.jpg'],'state':'present','stat':[20,1,2]})+'\n')
            digest = hashlib.sha256(frozen.read_bytes()).hexdigest()
            objects = Path(tmp)/'objects.jsonl'
            objects.write_text(json.dumps({'object_sha256':DIGEST,'length':20})+'\n')
            result = exact_matches(db,frozen,digest,objects)
            self.assertEqual(result['counts'],{'exact_match_in_verified_scope':1})
            self.assertNotIn('asset.jpg',json.dumps(result))
            index.apply_batch(db,'one','v2',[observation(mtime=4,sha256=DIGEST)])
            result = exact_matches(db,frozen,digest,objects)
            self.assertEqual(result['counts'],{'no_exact_match_in_verified_scope':1})
            self.assertEqual(result['inventory_sha_verified_at_frozen_revision'],0)
            with self.assertRaisesRegex(ValueError,'checksum'):
                exact_matches(db,frozen,'b'*64,objects)

    def test_replay_reuse_modification_and_absence(self):
        with tempfile.TemporaryDirectory() as tmp, index.connect(Path(tmp)/'index.sqlite3', write=True) as db:
            index.register_sources(db, [{'source_id':'one','device_id':'test','logical_root':'first'},
                                        {'source_id':'two','device_id':'test','logical_root':'second'}])
            first = [observation(sha256=DIGEST, metadata={'model':'camera'}), observation('other.jpg')]
            batch = index.apply_batch(db,'one','v1',first,coverage='complete')
            index.apply_batch(db,'two','v1',[observation()])
            self.assertTrue(index.apply_batch(db,'one','v1',first,coverage='complete')['reused'])
            with self.assertRaisesRegex(ValueError,'different content'):
                index.apply_batch(db,'one','v1',[observation('another.jpg')],coverage='complete')
            index.apply_batch(db,'one','v2',[observation()])
            by_source = index.status(db)['sources']
            self.assertEqual(by_source['one']['present'],2)
            self.assertEqual(by_source['one']['sha_verified'],1)
            self.assertEqual(by_source['two']['l0_pending'],1)
            index.apply_batch(db,'one','v3',[{'path':'asset.jpg','state':'unavailable','issue':'share offline'}])
            self.assertEqual(index.files(db)[0]['evidence'],{})
            index.apply_batch(db,'one','v4',[observation()])
            self.assertEqual(index.files(db)[0]['evidence']['sha256'],DIGEST)
            index.apply_batch(db,'one','v5',[observation(mtime=3)],coverage='complete')
            changed, missing, separate = index.files(db)
            self.assertEqual((changed['change_kind'],missing['state'],separate['state']),('changed','missing','present'))
            self.assertNotIn('sha256',changed['evidence'])
            self.assertEqual(index.apply_batch(db,'one','v1',first,coverage='complete')['batch_id'],batch['batch_id'])
            self.assertEqual(index.files(db)[0]['mtime_ns'],3)

    def test_batch_rejection_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp, index.connect(Path(tmp)/'index.sqlite3', write=True) as db:
            index.register_sources(db, [{'source_id':'one','device_id':'test','logical_root':'first'}])
            index.apply_batch(db,'one','v1',[observation('z.jpg',sha256=DIGEST)])
            with self.assertRaisesRegex(ValueError,'Conflicting'):
                index.apply_batch(db,'one','v2',[observation('a.jpg'), observation('z.jpg',sha256='b'*64)])
            self.assertEqual(len(index.files(db)),1)
            self.assertEqual(db.execute('SELECT count(*) FROM organize_batches').fetchone()[0],1)

    def test_existing_unrelated_database_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'index.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE production_only(id)')
            with self.assertRaises(ValueError):
                with index.connect(path,write=True):
                    self.fail('Unexpected writable production connection')
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [('production_only',)])


class DecisionsTest(unittest.TestCase):
    def test_pixel_identical_encodings_remain_in_confirmed_retain_export(self):
        with tempfile.TemporaryDirectory() as tmp, index.connect(Path(tmp)/'audit.sqlite3',write=True) as db:
            index.register_sources(db,[{'source_id':'one','device_id':'test','logical_root':'first'}])
            visual = {'status':'valid','pipeline_key':'test','pixel_sha256':'c'*64}
            index.apply_batch(db,'one','v1',[
                observation('capture.jpg',sha256=DIGEST,visual=visual,metadata={'model':'camera'}),
                observation('capture.png',sha256='b'*64,visual=visual,metadata={'make':'maker'}),
            ],coverage='complete')
            output = Path(tmp)/'plan'
            organize.output_plan(db,output)
            retained = [json.loads(line) for line in (output/'confirmed-retain.jsonl').read_text().splitlines()]
            self.assertEqual({tuple(row['source']) for row in retained},{('one','capture.jpg'),('one','capture.png')})
            self.assertEqual((output/'future-cleanup-review.jsonl').read_text(),'')

    def test_exact_decision_prefers_verified_decode(self):
        a = item('a.jpg', decode='failed', sha256=DIGEST)
        b = item('b.jpg', sha256=DIGEST)
        result = decisions.recommendation([a,b],'L0')
        self.assertEqual(result['preferred'],['restored','b.jpg'])
        self.assertFalse(result['execution_allowed'])
        self.assertEqual(result['metadata'],[])

    def test_direct_review_recommends_original_and_trusted_fields(self):
        original = item('original.jpg',width=1440,height=1080,quality={'derivation':'original'},
                        metadata={'date_time_original':'1970:01:01 00:00:00'})
        small = item('small.jpg',source='onedrive',quality={'derivation':'thumbnail'},
                    metadata={'date_time_original':'2019:04:05 12:30:40','gps':[24.6,120.1],
                              'orientation':'ignore','model':'unreviewed'},
                    trusted_fields=['date_time_original','gps','orientation'])
        inflated = item('inflated.jpg',width=6000,height=5000,quality={'derivation':'upscaled'},
                        metadata={'date_time_original':'2019:04:05 12:30:40'})
        def revision(row):
            return [*decisions.identity(row),row['size'],row['mtime_ns'],row['ctime_ns']]
        def edge(a,b):
            return {'a':revision(a),'b':revision(b),'review_id':'fixture-review',
                    'status':'confirmed_same_capture'}
        with self.assertRaisesRegex(ValueError,'direct'):
            decisions.recommendation([small,original,inflated],'reviewed_same_capture',
                                     [edge(original,small),edge(small,inflated)])
        result = decisions.recommendation([small,original,inflated],'reviewed_same_capture',
                                          [edge(original,small),edge(original,inflated)])
        self.assertEqual(result['preferred'],['restored','original.jpg'])
        self.assertEqual([row['field'] for row in result['metadata']],['date_time_original','gps'])
        self.assertTrue(all(row['source']==['onedrive','small.jpg'] for row in result['metadata']))
        self.assertTrue(all(not row['write_authorized'] for row in result['metadata']))
        inflated['evidence']['metadata']['date_time_original'] = '2020:01:01 00:00:00'
        result = decisions.recommendation([small,original,inflated],'reviewed_same_capture',
                                          [edge(original,small),edge(original,inflated)])
        date = result['metadata'][0]
        self.assertTrue(date['conflict'])
        self.assertIsNone(date['proposed'])

    def test_timezone_conflict_is_not_resolved_by_majority(self):
        common = {'date_time_original':'2019:04:05 12:30:40'}
        a = item('a.jpg',metadata={**common,'offset_time_original':'+08:00'})
        b = item('b.jpg',metadata={**common,'offset_time_original':'-07:00'},
                 trusted_fields=['date_time_original'])
        proposals = decisions.proposals(a,[a,b],'reviewed_same_capture')
        self.assertTrue(proposals[0]['conflict'])
        self.assertIsNone(proposals[0]['proposed'])

    def test_output_plan_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp, index.connect(Path(tmp)/'index.sqlite3', write=True) as db:
            index.register_sources(db, [{'source_id':'restored','device_id':'test','logical_root':'first'}])
            index.apply_batch(db,'restored','v1',[observation(sha256=DIGEST),observation('other.jpg')])
            left,right = (Path(tmp)/name for name in ('first','second'))
            organize.output_plan(db,left)
            organize.output_plan(db,right)
            self.assertEqual({p.name:p.read_bytes() for p in left.iterdir()},
                             {p.name:p.read_bytes() for p in right.iterdir()})
            with self.assertRaises(ValueError):
                organize.output_plan(db,left)

    def test_l1_requires_same_pipeline_and_same_pixels(self):
        visual = {'status':'valid','width':100,'height':100,'pipeline_key':'verified','pixel_sha256':DIGEST}
        a = item('a.jpg',visual=visual)
        b = item('b.jpg',visual={**visual,'pipeline_key':'other'})
        with self.assertRaises(ValueError):
            decisions.recommendation([a,b],'L1')
        b['evidence']['visual']['pipeline_key'] = 'verified'
        self.assertEqual(decisions.recommendation([a,b],'L1')['preferred'],['restored','a.jpg'])
        with self.assertRaises(ValueError):
            decisions.recommendation([a,b],'L2')

    def test_simulation_accounts_for_every_input_and_collisions(self):
        rows = [item('A.jpg',sha256=DIGEST),item('B.jpg',sha256=DIGEST),item('a.JPG'),
                item('album'),item('Album/p.jpg'),item('only-low-res.jpg',metadata={'date_time_original':'1970:01:01 00:00:00'})]
        rows.append(dict(item('offline.jpg'),state='missing'))
        result, metadata, summary = decisions.simulation(rows)
        self.assertEqual(len(result),len(rows))
        self.assertEqual(summary['l0_logical_redundancy'],20)
        self.assertEqual(summary['unavailable_or_missing'],1)
        self.assertEqual(summary['destination_renames'],3)
        destinations = {decisions.portable_key(r['proposed_destination']) for r in result if r['state']=='present'}
        self.assertEqual(len(destinations),6)
        self.assertEqual(decisions.destinations(rows[:-1]), decisions.destinations(list(reversed(rows[:-1]))))
        self.assertIsNone(result[-1]['proposed_destination'])
        self.assertEqual(result[-2]['action'],'retain_pending_search')
        self.assertTrue(all(not r['execution_allowed'] for r in result))
        self.assertEqual(metadata,[])

    def test_lookup_collapses_exact_references(self):
        query = item('a.jpg',sha256=DIGEST)
        refs = [item('b.jpg',source='onedrive',sha256=DIGEST),item('c.jpg',source='onedrive',sha256=DIGEST)]
        result = organize.lookup([query,*refs],['restored','a.jpg'],{'onedrive'},5)
        self.assertEqual(result['present_reference_paths'],2)
        self.assertEqual(result['total_distinct_candidates'],1)
        self.assertEqual(result['candidates'][0]['relation'],'L0')


class ExerciseTest(unittest.TestCase):
    def test_recovery_and_replay(self):
        self.assertEqual(exercise()['recovered_files'],2)

    def test_image_rehearsal_copies_exif_pixels_and_rolls_back(self):
        try:
            import PIL
        except ImportError:
            self.skipTest('Pillow is optional')
        from nas_photo.organize_exercise import exercise_images
        exiftool = Path(__file__).resolve().parent.parent/'tools/exiftool/exiftool'
        if not exiftool.is_file():
            self.skipTest('Project ExifTool unavailable')
        report = exercise_images(exiftool)
        self.assertTrue(report['decoded_pixels_identical'])
        self.assertEqual(report['restore_manifest'],'verified')
        self.assertEqual(report['portable_renames'],2)


class EvidenceTest(unittest.TestCase):
    def bundle(self, rows, relation='L0'):
        a,b = rows
        inputs = [{'revision':list(evidence.revision(row)), 'roles':[role],
                   'inventory_version':row['version']} for row,role in ((a,'query'),(b,'reference'))]
        return {'schema':'organize-evidence-v1','run_id':'fixture-run','producer':'DSH',
                'inputs':inputs,'inputs_sha256':evidence.digest(inputs),
                'expected_counts':{'queries':1,'references':1,'edges':1},
                'scope_versions':[a['version']],'scope_description':'fixture pair',
                'queries':[{'revision':inputs[0]['revision'],'status':'matched','reason':'fixture'}],
                'edges':[{'a':inputs[0]['revision'],'b':inputs[1]['revision'],'relation':relation,'reason':'fixture'}]}

    def test_g2_identity_distance_profiles_and_candidate_only(self):
        rows = [item('a.jpg'),item('b.jpg')]
        bundle = self.bundle(rows,'visual_candidate')
        bundle.update(algorithm={'implementation':'ImageHash','version':'fixture','pipeline':'same','bits':64},top_k=5)
        for row in bundle['inputs']:
            row.update(pipeline='same',phash='0'*16,dhash='0'*16,original_dimensions=[180,120],
                       oriented_dimensions=[180,120],sample_dimensions=[32,32])
        bundle['inputs_sha256'] = evidence.digest(bundle['inputs'])
        bundle['edges'][0].update(phash_distance=0,dhash_distance=0,rank=1)
        self.assertEqual(evidence.validate(bundle,rows)['edges'],1)
        bundle['edges'][0]['dhash_distance']=1
        with self.assertRaisesRegex(ValueError,'distance'):
            evidence.validate(bundle,rows)
        bundle['edges'][0]['dhash_distance']=0
        bundle['inputs'][1]['pipeline']='draft'
        bundle['inputs_sha256']=evidence.digest(bundle['inputs'])
        with self.assertRaisesRegex(ValueError,'preprocessing'):
            evidence.validate(bundle,rows)
        bundle['edges'][0].update(relation='confirmed_same_capture',review_id='not-human')
        with self.assertRaisesRegex(ValueError,'human'):
            evidence.validate(bundle,rows)

    def test_persist_replay_and_invalidate_by_revision(self):
        with tempfile.TemporaryDirectory() as tmp, index.connect(Path(tmp)/'index.sqlite3',write=True) as db:
            index.register_sources(db,[{'source_id':'restored','device_id':'test','logical_root':'fixture'}])
            index.apply_batch(db,'restored','v1',[observation('a.jpg',sha256=DIGEST),observation('b.jpg',sha256=DIGEST)])
            bundle = self.bundle(index.files(db))
            self.assertFalse(evidence.ingest(db,bundle)['reused'])
            self.assertTrue(evidence.ingest(db,bundle)['reused'])
            index.apply_batch(db,'restored','v2',[observation('a.jpg')])
            self.assertEqual(len(evidence.current(db,index.files(db))[0]),1)
            index.apply_batch(db,'restored','v3',[observation('a.jpg',mtime=3)])
            self.assertEqual(evidence.current(db,index.files(db))[2],['fixture-run'])

    def test_czkawka_direct_report_keeps_its_own_metric(self):
        rows = [item('a.jpg'),item('b.jpg')]
        bundle = self.bundle(rows,'visual_candidate')
        bundle['algorithm'] = {'implementation':'Czkawka CLI','version':'12.0.2','image':'pinned',
                               'profile':'test','metric':'czkawka_reported_distance','max_difference':10}
        bundle['top_k'] = 5
        edge = bundle['edges'][0]
        edge.update(distance=4,rank=1)
        export = [[{'path':'/samples/ref/a.jpg'},[{'path':'/samples/probe/b.jpg','difference':4}]]]
        bundle['scan_runs'] = [{'query':edge['a'],'anchor':'/samples/ref/a.jpg',
                                'references':[{'revision':edge['b'],'alias':'/samples/probe/b.jpg'}],
                                'export':export,'export_digest':evidence.digest(export)}]
        self.assertEqual(evidence.validate(bundle,rows)['edges'],1)
        edge['distance']=5
        with self.assertRaisesRegex(ValueError,'distance'):
            evidence.validate(bundle,rows)

    def test_reconcile_requires_hash_and_respects_partial_coverage(self):
        def snapshot(scope, complete, files):
            return {'scope':scope,'version':'v1','complete':complete,'files':[
                {'path':p,'size':s,'sha256':h} for p,s,h in files]}
        left = snapshot('US',True,[('photo.jpg',10,DIGEST),('other.jpg',8,DIGEST),('size.jpg',9,None)])
        right = snapshot('CN',False,[('photo.jpg',10,'b'*64),('alias.jpg',10,DIGEST),('size.jpg',9,None)])
        plan = reconcile(left,right)
        result = {row['path']:row for row in plan['rows']}
        self.assertEqual(result['photo.jpg']['state'],'sha_mismatch')
        self.assertEqual(result['photo.jpg']['reusable_destination_paths'],['alias.jpg'])
        self.assertEqual(result['other.jpg']['state'],'destination_not_observed')
        self.assertEqual(result['size.jpg']['state'],'same_size_sha_unverified')
        self.assertFalse(plan['execution_allowed'])
        self.assertFalse(plan['all_content_verified'])
        complete = snapshot('US',True,[('photo.jpg',10,DIGEST)])
        self.assertTrue(reconcile(complete,{**complete,'scope':'CN'})['all_content_verified'])

    def test_bad_copy_never_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'input'
            source.write_bytes(b'expected')
            destination = root/'out'
            destination.mkdir()
            with self.assertRaises(ValueError):
                verified_copy(source,destination,'nested/output.jpg','a'*64)
            self.assertEqual(list((destination/'nested').iterdir()),[])
            self.assertEqual(verified_copy(source,destination,'nested/output.jpg',checksum(source)),'copied_verified')

    def test_exiftool_rehearsal_preserves_decoded_pixels(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('Optional Pillow dependency unavailable')
        exiftool = Path(__file__).resolve().parent.parent/'tools/exiftool/exiftool'
        if not exiftool.is_file():
            self.skipTest('Project ExifTool unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            original, copy = Path(tmp)/'source.jpg',Path(tmp)/'copy.jpg'
            Image.new('RGB',(37,29),(40,20,120)).save(original,quality=91)
            copy.write_bytes(original.read_bytes())
            original_digest = checksum(original)
            subprocess.run([str(exiftool),'-overwrite_original','-DateTimeOriginal=2019:04:05 12:30:40',str(copy)],
                           check=True,capture_output=True,timeout=15)
            data = subprocess.check_output([str(exiftool),'-json','-DateTimeOriginal',str(copy)],timeout=15)
            self.assertEqual(json.loads(data)[0]['DateTimeOriginal'],'2019:04:05 12:30:40')
            self.assertEqual(checksum(original),original_digest)
            self.assertNotEqual(checksum(copy),original_digest)
            with Image.open(original) as a, Image.open(copy) as b:
                self.assertEqual(hashlib.sha256(a.tobytes()).digest(),hashlib.sha256(b.tobytes()).digest())


if __name__ == '__main__':
    unittest.main()
