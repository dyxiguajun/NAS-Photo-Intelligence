from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw, ImageEnhance, PngImagePlugin
import test_duplicates as fixtures
from nas_photo import similarity as sim
from nas_photo import duplicates as exact
from nas_photo.imaging import analyze_file, pipeline_key
from nas_photo.metadata import snapshot, fingerprint
from nas_photo.store import connect, encode
from nas_photo.process import run_worker
from worker_probe import controlled_worker
from nas_photo.migrations import V2, migrate
from nas_photo.store import SCHEMA

PROJECT=Path(__file__).resolve().parents[1]


def scene():
    im=Image.new('RGB',(600,400))
    im.putdata([((x*3+y)%256,(x+y*2)%256,(x//3+y//2)%256) for y in range(400) for x in range(600)])
    draw=ImageDraw.Draw(im)
    for n in range(25):
        x=(n*173)%530;y=(n*67)%330
        draw.ellipse((x,y,x+50,y+60), fill=((n*43)%256,(n*91)%256,(n*127)%256))
    draw.rectangle((20,20,200,50),fill='white');draw.text((28,28),'LOCAL SYNTHETIC TEST',fill='black')
    return im


def image_bytes(im,fmt='PNG',**kw):
    out=io.BytesIO();im.save(out,format=fmt,**kw);return out.getvalue()


class SimilarityTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DuplicateTests();self.fixture.setUp()
        self.config=self.fixture.config; self.base=self.fixture.base; self.photos=self.fixture.photos
        self.opts=sim.options(self.config);self.im=scene()
    def tearDown(self): self.im.close();self.fixture.tearDown()
    def seed(self,name,im=None,fmt='PNG',**kw):
        return self.fixture.seed(name,image_bytes(im if im is not None else self.im,fmt,**kw))
    def run_visual(self,items,**kw):
        jid=sim.create_job(self.config,items,sim.options(self.config,**kw))
        return jid,sim.run_job(self.config,jid)
    def db(self): return closing(exact.readonly(self.config))

    def test_metadata_only_pixel_equality_and_binary_representative(self):
        info=PngImagePlugin.PngInfo();info.add_text('Comment','different metadata')
        items=[self.seed('a.png'),self.seed('metadata.png',pnginfo=info),self.seed('copy.png')]
        hj=exact.create_hash_job(self.config,items,exact.options(self.config,include_non_candidates=True));exact.run_hash_job(self.config,hj)
        jid,r=self.run_visual(items)
        self.assertEqual(r['items'],{'done':2,'reused_binary':1});self.assertEqual(r['decode_count'],2)
        c=sim.compare(self.config,jid);self.assertEqual(c['counts']['pixel_equivalent'],3)
        report=sim.report(self.config,jid)
        self.assertEqual(report['pixel_equivalent_group_count'],1);self.assertEqual(report['visual_candidate_count'],0)
        with self.db() as db:
            hashes=[r[0] for r in db.execute('SELECT digest FROM hash_observations ORDER BY hash_id')]
            self.assertNotEqual(hashes[0],hashes[1]);self.assertEqual(hashes[0],hashes[2])
        self.assertEqual(len(sim.export_report(self.config,jid)['outputs']),3)

    def test_report_binary_groups_respect_visual_manifest_scope(self):
        items=[self.seed('a.png'),self.seed('b.png'),self.seed('c.png')]
        hj=exact.create_hash_job(self.config,items,exact.options(self.config,include_non_candidates=True));exact.run_hash_job(self.config,hj)
        jid,_=self.run_visual(items[:1])
        self.assertEqual(sim.report(self.config,jid)['binary_duplicates'],[])
        jid,_=self.run_visual(items[:2])
        self.assertEqual(sim.report(self.config,jid)['binary_duplicates'][0]['member_count'],2)

    def test_basic_variants_and_explicit_rotation(self):
        items=[self.seed('reference.png'),self.seed('q95.jpg',fmt='JPEG',quality=95),self.seed('q60.jpg',fmt='JPEG',quality=60),
               self.seed('small.png',self.im.resize((300,200),Image.Resampling.LANCZOS)),
               self.seed('bright.png',ImageEnhance.Brightness(self.im).enhance(1.05)),
               self.seed('rotate.png',self.im.transpose(Image.Transpose.ROTATE_90))]
        jid,r=self.run_visual(items);self.assertEqual(r['decode_count'],6)
        sim.compare(self.config,jid);report=sim.report(self.config,jid)
        first=report['members'][0]['file_id']
        edges=[r for r in report['relations'] if r['a_file_id']==first]
        self.assertEqual(len(edges),5);self.assertTrue(all(r['kind']=='visual_similarity_candidate' for r in edges))
        self.assertEqual(report['pixel_equivalent_group_count'],0)

    def test_exif_orientation_is_pixel_equivalent_and_alpha_preserved(self):
        exif=Image.Exif();exif[274]=6
        rotated=self.im.transpose(Image.Transpose.ROTATE_90)
        a=self.seed('normal.png');b=self.seed('oriented.png',rotated,exif=exif)
        jid,_=self.run_visual([a,b]);sim.compare(self.config,jid)
        self.assertEqual(sim.report(self.config,jid)['pixel_equivalent_group_count'],1)
        rgba=self.im.convert('RGBA');transparent=rgba.copy();transparent.putalpha(100)
        jid,_=self.run_visual([self.seed('opaque.png',rgba),self.seed('alpha.png',transparent)])
        sim.compare(self.config,jid);self.assertEqual(sim.report(self.config,jid)['pixel_equivalent_group_count'],0)

    def test_unsupported_result_cache_keeps_unanalysed_status(self):
        # A valid animated PNG is unsupported as a whole, rather than treated as its first frame.
        data=io.BytesIO();self.im.save(data,format='PNG',save_all=True,append_images=[self.im.transpose(Image.Transpose.ROTATE_90)])
        item=self.fixture.seed('animated.png',data.getvalue())
        _,first=self.run_visual([item]);self.assertEqual(first['items'],{'unsupported':1})
        _,cached=self.run_visual([item],max_read_bytes=0)
        self.assertEqual(cached['items'],{'unsupported':1});self.assertEqual(cached['program_read_bytes_observed'],0)
        self.assertEqual(cached['job']['status'],'completed')

    def test_simple_scenes_are_not_visual_matches(self):
        items=[self.seed('red.png',Image.new('RGB',(600,400),'red')),self.seed('green.png',Image.new('RGB',(600,400),'green'))]
        jid,_=self.run_visual(items);c=sim.compare(self.config,jid)
        self.assertEqual(c['counts']['low_information'],1);self.assertEqual(sim.report(self.config,jid)['visual_candidate_count'],0)

    def test_cache_threshold_change_and_force(self):
        items=[self.seed('a.png'),self.seed('jpeg.jpg',fmt='JPEG',quality=65)]
        jid,r=self.run_visual(items);second,cache=self.run_visual(items,max_read_bytes=0)
        self.assertEqual(cache['items'],{'skipped':2});self.assertEqual(cache['program_read_bytes_observed'],0)
        with patch('nas_photo.similarity.run_worker',side_effect=AssertionError('comparison must not read sources')):
            c=sim.compare(self.config,second,sim.comparison_options(phash=0,dhash=0))
        self.assertEqual(c['source_content_bytes_read'],0)
        _,force=self.run_visual(items,force=True);self.assertEqual(force['decode_count'],2)
        self.assertEqual(force['program_read_bytes_observed'],r['program_read_bytes_observed'])

    def test_changed_pixels_invalidate_current_and_historical_relations(self):
        info=PngImagePlugin.PngInfo();info.add_text('Comment','x')
        a,b=self.seed('a.png'),self.seed('b.png',pnginfo=info)
        jid,_=self.run_visual([a,b]);sim.compare(self.config,jid)
        self.assertEqual(sim.report(self.config,jid)['pixel_equivalent_group_count'],1)
        (self.photos/'b.png').write_bytes(image_bytes(Image.new('RGB',(600,400),'red')))
        self.run_visual([b])
        report=sim.report(self.config,jid)
        self.assertEqual(report['pixel_equivalent_group_count'],0);self.assertEqual(report['invalidated_historical_relations'],1)

    def test_identity_change_invalidates_even_when_size_and_mtime_preserved(self):
        item=self.seed('a.png');jid,_=self.run_visual([item]);path=self.photos/'a.png';old=path.stat()
        replacement=path.with_name('new.png');replacement.write_bytes(path.read_bytes());os.utime(replacement,ns=(old.st_atime_ns,old.st_mtime_ns));os.replace(replacement,path)
        _,r=self.run_visual([item]);self.assertEqual(r['decode_count'],1)

    def test_normalization_version_change_invalidates_cache(self):
        item=self.seed('a.png');self.run_visual([item]);opts=sim.options(self.config);opts['pipeline']['normalization']='future-policy'
        opts['pipeline_key']=pipeline_key(opts['pipeline']);jid=sim.create_job(self.config,[item],opts)
        with self.assertRaisesRegex(ValueError,'version changed'): sim.run_job(self.config,jid)
        with patch('nas_photo.similarity.pipeline',return_value=opts['pipeline']):
            def worker(req,timeout):
                if req['operation']=='visual':
                    with patch('nas_photo.imaging.pipeline',return_value=opts['pipeline']): return {'ok':True,'result':analyze_file(req)}
                return run_worker(req,timeout)
            r=sim.run_job(self.config,jid,worker_fn=worker)
        self.assertEqual(r['decode_count'],1)

    def test_corrupt_oversized_video_and_pixel_guard(self):
        corrupt=self.fixture.seed('corrupt.png',b'bad image')
        video=self.fixture.seed('clip.mov',b'video')
        image=self.seed('big.png')
        opts=dict(self.opts,max_pixels=100)
        jid=sim.create_job(self.config,[corrupt,video,image],opts);r=sim.run_job(self.config,jid)
        self.assertEqual(r['items'],{'failed':1,'limited':1,'unsupported':1})
        self.assertEqual(r['decode_count'],0)
        with self.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM visual_observations WHERE status='valid'").fetchone()[0],0)
        opts=dict(self.opts,max_file_bytes=1);jid=sim.create_job(self.config,[image],opts);r=sim.run_job(self.config,jid)
        self.assertEqual(r['program_read_bytes_observed'],0);self.assertEqual(r['items'],{'limited':1})

    def test_budget_survives_resume_and_cancel(self):
        a,b=self.seed('a.png'),self.seed('b.png')
        size=(self.photos/'a.png').stat().st_size
        jid=sim.create_job(self.config,[a,b],dict(self.opts,max_read_bytes=size))
        r=sim.run_job(self.config,jid);self.assertEqual(r['items'],{'done':1,'pending':1});self.assertEqual(r['job']['status'],'budget_exhausted')
        r=sim.run_job(self.config,jid);self.assertEqual(r['program_read_bytes_observed'],size)
        jid=sim.create_job(self.config,[b],self.opts)
        def cancel(req,timeout): raise KeyboardInterrupt
        r=sim.run_job(self.config,jid,worker_fn=cancel);self.assertEqual(r['job']['status'],'cancelled')
        r=sim.run_job(self.config,jid);self.assertEqual(r['items'],{'done':1})

    def test_unavailable_mount_resume_and_original_identity(self):
        item=self.seed('a.png');jid=sim.create_job(self.config,[item],self.opts)
        renamed=self.photos.with_name('remapped');self.photos.rename(renamed)
        r=sim.run_job(self.config,jid);self.assertEqual(r['items'],{'failed':1})
        renamed.rename(self.photos);r=sim.run_job(self.config,jid,retry_failed=True);self.assertEqual(r['items'],{'done':1})
        with self.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0],1)

    def test_nontransitive_pairs_are_never_union_groups(self):
        # Three stored fingerprints with distances A-B=4, B-C=4, A-C=8.
        items=[self.seed('a.png'),self.seed('b.png',ImageEnhance.Brightness(self.im).enhance(1.02)),self.seed('c.png',ImageEnhance.Brightness(self.im).enhance(1.04))]
        jid,_=self.run_visual(items)
        db=connect(self.config)
        with db:
            for row,ph in zip(db.execute('SELECT * FROM visual_observations ORDER BY visual_id').fetchall(),('0000000000000000','000000000000000f','00000000000000ff')):
                r=json.loads(row['result_json'])
                for v in r['perceptual']:v['phash']=ph;v['dhash']='0000000000000000'
                db.execute('UPDATE visual_observations SET result_json=? WHERE visual_id=?',(encode(r),row['visual_id']))
        db.close();c=sim.compare(self.config,jid,sim.comparison_options(phash=4))
        self.assertEqual(c['counts']['visual_similarity_candidate'],2)
        report=sim.report(self.config,jid);self.assertEqual(report['pixel_equivalent_group_count'],0)
        self.assertEqual(len(report['relations']),2)

    def test_worker_timeout_reservation_and_retry(self):
        item=self.seed('a.png');jid=sim.create_job(self.config,[item],self.opts)
        def worker(req,timeout):
            if req['operation']=='visual':
                p=Path(req['progress_path']);p.write_bytes(b'000000000000000000000123')
                return {'ok':False,'error':{'code':'timeout','message':'injected decoder hang'}}
            return run_worker(req,timeout)
        r=sim.run_job(self.config,jid,worker_fn=worker)
        self.assertEqual(r['items'],{'failed':1});self.assertFalse(r['read_bytes_are_exact'])
        self.assertEqual(r['program_read_bytes_observed'],246)
        self.assertEqual(r['read_budget_charged_bytes'],2*(self.photos/'a.png').stat().st_size)
        r=sim.run_job(self.config,jid,retry_failed=True);self.assertEqual(r['items'],{'done':1})

    def test_hard_kill_preserves_completed_and_reserved_bytes(self):
        first=self.seed('a.png');noise=Image.frombytes('RGB',(1200,1000),os.urandom(3_600_000));second=self.seed('slow.png',noise)
        jid=sim.create_job(self.config,[first,second],self.opts)
        inject=self.base/'inject';inject.mkdir()
        marker=self.base/'worker-identity.json'
        (inject/'sitecustomize.py').write_text("""import sys
if '-m' in sys.orig_argv and 'nas_photo.worker' in sys.orig_argv:
 import nas_photo.imaging as m, time, os, json
 from pathlib import Path
 old=m.analyze_file
 def run(req):
  if req.get('relative_path')=='slow.png':
   Path(os.environ['NAS_PHOTO_HARDKILL_MARKER']).write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp()}))
   original=m.read_chunk
   def read(fd,n):
    data=original(fd,n); time.sleep(.7); return data
   m.read_chunk=read
  return old(req)
 m.analyze_file=run
""")
        env={**os.environ,'PYTHONPATH':str(inject)+os.pathsep+str(PROJECT/'src'),'NAS_PHOTO_HARDKILL_MARKER':str(marker)}
        p=subprocess.Popen([sys.executable,str(PROJECT/'nas_photo_cli.py'),'--config',str(self.config.path),'similarity','resume',jid],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            end=time.monotonic()+20
            while time.monotonic()<end:
                with self.db() as db:
                    done=db.execute("SELECT count(*) FROM job_items WHERE job_id=? AND status='done'",(jid,)).fetchone()[0]
                    row=db.execute('SELECT progress_path FROM job_items WHERE job_id=? AND progress_path IS NOT NULL',(jid,)).fetchone()
                if done==1 and row and int(Path(row[0]).read_bytes())>0:break
                time.sleep(.1)
            else:self.fail('did not observe isolated in-flight decode')
            owned=json.loads(marker.read_text())
            self.assertEqual(owned['pid'],owned['pgid'])
            p.kill();p.wait(timeout=3)
            end=time.monotonic()+5
            while time.monotonic()<end:
                probe=subprocess.run(['/bin/ps','-o','stat=','-p',str(owned['pid'])],capture_output=True,text=True,timeout=1)
                if not probe.stdout.strip() or probe.stdout.strip().startswith('Z'):break
                time.sleep(.05)
            else:self.fail('owned worker remained runnable after its supervisor died')
            r=sim.run_job(self.config,jid);self.assertEqual(r['items'],{'done':2});self.assertFalse(r['read_bytes_are_exact'])
            self.assertEqual(r['decode_count'],2)
            self.assertGreater(r['read_budget_charged_bytes'],r['program_read_bytes_observed'])
        finally:
            if p.poll() is None:p.kill();p.wait(timeout=3)
            p.stderr.close()

    def test_real_timeout_and_rss_guard_only_own_worker(self):
        item=self.seed('a.png');fs,identity=snapshot(self.photos/'a.png');counter=self.base/'counter';counter.write_bytes(b'0'*24)
        request={**self.opts,'operation':'visual','root':str(self.photos),'relative_path':'a.png','expected_fingerprint':fingerprint(fs),
                 'expected_identity':list(identity),'progress_path':str(counter),'read_allowance':fs['size']}
        self.assertEqual(run_worker(request,.001)['error']['code'],'timeout')
        sentinel=subprocess.Popen([sys.executable,'-c','import sys; sys.stdin.readline()'],stdin=subprocess.PIPE,start_new_session=True)
        try:
            result,trace=controlled_worker(request)
            self.assertFalse(result['ok'],trace)
            self.assertEqual(result['error']['code'],'memory_limit',trace)
            self.assertGreater(trace['sampled_max_RSS_KiB'],128*1024,trace)
            self.assertTrue(all(w['reaped'] for w in trace['workers']),trace)
            pid=trace['workers'][0]['pid']
            self.assertEqual(trace['markers']['ready']['pgid'],pid)
            self.assertEqual({x['pgid'] for x in trace['signals']},{pid})
            self.assertIsNone(sentinel.poll(),'unrelated sentinel was terminated')
        finally:
            sentinel.communicate(b'finish\n',timeout=3)

    def test_normal_memory_worker_completes_below_rss_limit(self):
        result,trace=controlled_worker(mode='normal',allocation_mib=8)
        self.assertTrue(result['ok'],trace)
        self.assertTrue(result['result']['fixture_completed'],trace)
        self.assertLess(trace['sampled_max_RSS_KiB'],128*1024,trace)
        self.assertEqual(trace['signals'],[],trace)
        self.assertTrue(all(w['reaped'] for w in trace['workers']),trace)

    def test_crash_and_sigterm_ignoring_worker_are_reaped(self):
        result,trace=controlled_worker(mode='crash',allocation_mib=0)
        self.assertFalse(result['ok'],trace)
        self.assertEqual(result['error']['code'],'worker_crash',trace)
        self.assertEqual(trace['workers'][0]['returncode'],7,trace)
        result,trace=controlled_worker(mode='ignore_term',allocation_mib=8,timeout=.9)
        self.assertFalse(result['ok'],trace)
        self.assertEqual(result['error']['code'],'timeout',trace)
        self.assertEqual(trace['workers'][0]['returncode'],-signal.SIGKILL,trace)
        self.assertTrue(all(w['reaped'] for w in trace['workers']),trace)

    def test_actual_rss_failure_keeps_commits_and_allows_bounded_recovery(self):
        items=[self.seed('completed.png'),self.seed('over-memory.png')]
        jid=sim.create_job(self.config,items,self.opts)
        def worker(req,timeout):
            if req['operation']=='visual' and req['relative_path']=='over-memory.png':
                return controlled_worker(req,timeout=min(timeout,10))[0]
            return run_worker(req,timeout)
        r=sim.run_job(self.config,jid,worker_fn=worker)
        self.assertEqual(r['items'],{'done':1,'limited':1})
        self.assertFalse(r['read_bytes_are_exact'])
        with self.db() as db:
            committed=db.execute("SELECT visual_id FROM visual_observations WHERE status='valid'").fetchone()[0]
            self.assertEqual(db.execute("SELECT count(*) FROM visual_observations WHERE status='valid'").fetchone()[0],1)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        # A resource-limited item is terminal-unanalysed, not an ordinary pending
        # item: after the resource issue is removed, use a new explicit job.
        _,recovered=self.run_visual(items[1:])
        self.assertEqual(recovered['items'],{'done':1})
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status FROM visual_observations WHERE visual_id=?',(committed,)).fetchone()[0],'valid')
            self.assertEqual(db.execute("SELECT count(*) FROM visual_observations WHERE status='valid'").fetchone()[0],2)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_heif_decode_and_icc_policy(self):
        import pillow_heif
        from PIL import ImageCms
        pillow_heif.register_heif_opener()
        profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        items=[self.seed('a.png'),self.seed('icc.png',icc_profile=profile),self.seed('heic.heic',fmt='HEIF',quality=95)]
        jid,r=self.run_visual(items);self.assertEqual(r['decode_count'],3)
        sim.compare(self.config,jid);report=sim.report(self.config,jid)
        heif=next(m for m in report['members'] if m['relative_path']=='heic.heic')
        self.assertEqual(heif['visual']['format'],'HEIF')
        self.assertTrue(any(r['kind']=='visual_similarity_candidate' for r in report['relations']))
        bad=self.seed('bad-icc.png',icc_profile=b'not an ICC profile')
        _,r=self.run_visual([bad]);self.assertEqual(r['items'],{'failed':1})

    def test_phase2_new_hash_invalidates_same_stat_visual_cache(self):
        item=self.seed('a.png');jid,_=self.run_visual([item]);path=self.photos/'a.png'
        # Fixed-size BMP-like padding is not necessary: mutate one stored byte without changing metadata.
        old_stat=path.stat();data=bytearray(path.read_bytes());data[-1]^=1;path.write_bytes(data)
        os.utime(path,ns=(old_stat.st_atime_ns,old_stat.st_mtime_ns))
        hj=exact.create_hash_job(self.config,[item],exact.options(self.config,force=True,include_non_candidates=True))
        exact.run_hash_job(self.config,hj)
        _,r=self.run_visual([item]);self.assertEqual(r['decode_count'],1)
        self.assertEqual(r['items'],{'done':1})

    def test_file_change_during_read_never_publishes_fingerprint(self):
        item=self.seed('a.png');jid=sim.create_job(self.config,[item],self.opts)
        def worker(req,timeout):
            if req['operation']!='visual':return run_worker(req,timeout)
            from nas_photo import imaging
            original=imaging.read_chunk
            def change(fd,n):
                data=original(fd,n)
                with (self.photos/'a.png').open('ab') as f:f.write(b'changed')
                return data
            with patch('nas_photo.imaging.read_chunk',side_effect=change):return {'ok':True,'result':analyze_file(req)}
        r=sim.run_job(self.config,jid,worker_fn=worker)
        self.assertEqual(r['items'],{'failed':1})
        with self.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM visual_observations WHERE status='valid'").fetchone()[0],0)

    def test_v2_migration_preserves_every_old_row_and_rollback(self):
        path=self.base/'old.sqlite3';db=sqlite3.connect(path);db.executescript(SCHEMA);db.executescript(V2)
        db.execute("INSERT INTO sources VALUES('stable','nas','photos','legacy','old')");db.commit()
        tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before={t:db.execute('SELECT * FROM '+t).fetchall() for t in tables}
        migrate(db,path);migrate(db,path)
        for t,rows in before.items():
            if t!='schema_migrations':self.assertEqual(rows,db.execute('SELECT * FROM '+t).fetchall())
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok');self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[]);db.close()
        with closing(sqlite3.connect(path.with_name('old.pre-phase3.sqlite3'))) as b:self.assertEqual(b.execute('PRAGMA user_version').fetchone()[0],2)
        broken=self.base/'broken.sqlite3';db=sqlite3.connect(broken);db.executescript(SCHEMA);db.executescript(V2)
        with patch('nas_photo.visual_migration.V3','CREATE TABLE transient(x); INVALID SQL;'):
            with self.assertRaises(sqlite3.Error):migrate(db,broken)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
        self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='transient'").fetchone());db.close()


if __name__=='__main__':unittest.main()
