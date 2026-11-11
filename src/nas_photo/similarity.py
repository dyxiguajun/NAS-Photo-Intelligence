"""Durable bounded visual jobs and local, non-transitive fingerprint comparisons."""
from collections import Counter, defaultdict
import csv
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from . import duplicates as exact
from .engine import exclusive, log
from .imaging import pipeline, pipeline_key
from .process import run_worker
from .store import connect, encode, stamp


def options(config, force=False, max_read_bytes=None):
    result = {'max_read_bytes':256*1024**2, 'max_file_bytes':64*1024**2,
              'external_read_reserve_bytes':512*1024**2, 'max_pixels':16_000_000, 'max_dimension':16000, 'max_rss_mb':512,
              **config.raw.get('visual_limits', {}), 'force':bool(force)}
    if max_read_bytes is not None: result['max_read_bytes'] = max_read_bytes
    bounds = {'max_read_bytes':(0,2*1024**3), 'max_file_bytes':(1,64*1024**2),
              'external_read_reserve_bytes':(0,2*1024**3), 'max_pixels':(1,24_000_000), 'max_dimension':(1,32000), 'max_rss_mb':(128,768)}
    for k,(lo,hi) in bounds.items():
        if type(result[k]) is not int or not lo<=result[k]<=hi: raise ValueError('Invalid visual limit: '+k)
    result['pipeline'] = pipeline(); result['pipeline_key'] = pipeline_key(result['pipeline'])
    return result


def comparison_options(phash=8, dhash=12, aspect=.08, color=35, min_stddev=5):
    if not (type(phash) is int and type(dhash) is int and 0<=phash<=64 and 0<=dhash<=64
            and 0<=aspect<=.5 and 0<=color<=255 and 0<=min_stddev<=128): raise ValueError('Invalid comparison thresholds')
    return {'phash_max_hamming':phash, 'dhash_max_hamming':dhash, 'aspect_relative_tolerance':aspect,
            'rgb_mean_max_difference':color, 'min_luma_stddev':min_stddev, 'distance':'Hamming',
            'bits':64, 'policy':'pairwise-rotation-color-aspect-v1', 'candidate_probability':None}


def create_job(config, items, opts):
    if not 1<=len(items)<=100: raise ValueError('Phase 3 manifest must contain 1..100 indexed files')
    with exclusive(config):
        db = connect(config)
        try:
            rows = exact.scoped_files(db, items)
            # Keep the previous 15 and all newly indexed files inside this phase's total limit.
            if db.execute('SELECT count(*) FROM files').fetchone()[0]>100: raise ValueError('Phase 3 distinct-file scope exceeds 100; use a bounded project database')
            jid=uuid.uuid4().hex
            with db:
                db.execute("INSERT INTO jobs(job_id,status,created_at,updated_at,rules_json,limits_json,kind,options_json) VALUES (?,?,?,?,?,?,'visual',?)",
                           (jid,'queued',stamp(),stamp(),encode(config.rules),encode(config.limits),encode(opts)))
                for row in rows:
                    meta=json.loads(row['metadata_json'] or '{}').get('metadata',{})
                    video=meta.get('kind')=='video' or Path(row['relative_path']).suffix.lower() in ('.mov','.mp4','.m4v','.avi')
                    db.execute('INSERT INTO job_items(job_id,file_id,prior_json,status) VALUES (?,?,?,?)',
                               (jid,row['file_id'],encode({'indexed_size':row['indexed_size']}),'unsupported' if video else 'pending'))
            return jid
        finally: db.close()


def state(db, fid, status, vid=None):
    db.execute('''INSERT INTO visual_current(file_id,visual_id,status,checked_at) VALUES (?,?,?,?)
        ON CONFLICT(file_id) DO UPDATE SET visual_id=coalesce(excluded.visual_id,visual_current.visual_id),status=excluded.status,checked_at=excluded.checked_at''',
               (fid,vid,status,stamp()))


def fail(db, item, opts, error, attempts, read=0, before=None, status='failed'):
    if status=='failed': exact.current_state(db,item['file_id'],'unavailable')
    terminal=status in ('unsupported','limited') or item['attempts']>=attempts
    db.execute('UPDATE job_items SET status=?,last_error_json=? WHERE item_id=?',
               (status if terminal else 'pending',encode(error),item['item_id']))
    state(db,item['file_id'],status if status in ('limited','unsupported') else 'unavailable')
    db.execute('INSERT INTO errors(job_id,file_id,occurred_at,attempt,code,message) VALUES (?,?,?,?,?,?)',
               (item['job_id'],item['file_id'],stamp(),item['total_attempts'],error['code'],error['message']))
    cursor=db.execute('''INSERT INTO visual_observations(file_id,job_id,status,pipeline_key,before_json,read_bytes,completed_at,error_json)
               VALUES (?,?,?,?,?,?,?,?)''', (item['file_id'],item['job_id'],status,opts['pipeline_key'],encode(before),read,stamp(),encode(error)))
    if status in ('unsupported','limited'): state(db,item['file_id'],status,cursor.lastrowid)


def recover(db, config, item, opts, limits):
    observed=exact.recover_attempt(db,config,item)
    with db: fail(db,item,opts,{'code':'interrupted_visual_read','message':'no fingerprint published; observed bytes are a lower bound'},limits['max_attempts'],observed)


def save(db, item, opts, response, reused=None, binary=None):
    r=response['result']
    cur=db.execute('''INSERT INTO visual_observations(file_id,job_id,status,pipeline_key,fingerprint,before_json,result_json,
               pixel_sha256,width,height,read_bytes,completed_at,reused_visual_id,binary_hash_id)
               VALUES (?,?,'valid',?,?,?,?,?,?,?,?,?,?,?)''',
               (item['file_id'],item['job_id'],opts['pipeline_key'],response['fingerprint'],encode(response['before']),encode(r),
                r['pixel_sha256'],r['width'],r['height'],response['read_bytes'],stamp(),reused,binary))
    state(db,item['file_id'],'valid',cur.lastrowid)
    db.execute('UPDATE job_items SET status=?,last_error_json=NULL WHERE item_id=?',('reused_binary' if reused else 'done',item['item_id']))


def status(db, jid):
    row=db.execute("SELECT * FROM jobs WHERE job_id=? AND kind='visual'",(jid,)).fetchone()
    if row is None: raise ValueError('Unknown visual job_id')
    job=dict(row)
    for k in ('options_json','limits_json','rules_json'): job[k[:-5]]=json.loads(job.pop(k))
    counts=dict(db.execute('SELECT status,count(*) FROM job_items WHERE job_id=? GROUP BY status',(jid,)).fetchall())
    results=[json.loads(r[0]) for r in db.execute("SELECT result_json FROM visual_observations WHERE job_id=? AND status='valid' AND reused_visual_id IS NULL",(jid,))]
    return {'job':job,'items':counts,'scoped_files':sum(counts.values()),'program_read_bytes_observed':job['read_bytes'],
            'read_bytes_are_exact':bool(job['accounting_complete']), 'read_budget_charged_bytes':job['read_budget_used'],
            'unconfirmed_reserved_bytes':job['read_budget_used']-job['read_bytes'], 'cache_saved_content_bytes':job['cache_saved_bytes'],
            'decode_count':len(results), 'decode_seconds':sum(r['decode_seconds'] for r in results),
            'fingerprint_seconds':sum(r['fingerprint_seconds'] for r in results),
            'peak_worker_rss_bytes':max((r['peak_rss_bytes'] for r in results),default=0), 'network_transfer_bytes':None}


def run_job(config, jid, retry_failed=False, worker_fn=None):
    worker_fn=worker_fn or run_worker
    with exclusive(config):
        db=connect(config)
        row=db.execute("SELECT * FROM jobs WHERE job_id=? AND kind='visual'",(jid,)).fetchone()
        if row is None: db.close(); raise ValueError('Unknown visual job_id')
        opts,limits=json.loads(row['options_json']),json.loads(row['limits_json'])
        if pipeline_key(pipeline())!=opts['pipeline_key']: db.close(); raise ValueError('Decoder/config version changed; start a new bounded job')
        started=time.monotonic(); final='interrupted'
        try:
            for old in db.execute('SELECT * FROM job_items WHERE job_id=? AND progress_path IS NOT NULL',(jid,)).fetchall(): recover(db,config,dict(old),opts,limits)
            with db:
                db.execute("UPDATE job_items SET status='pending' WHERE job_id=? AND status='running'",(jid,))
                if retry_failed: db.execute("UPDATE job_items SET status='pending',attempts=0 WHERE job_id=? AND status='failed'",(jid,))
                db.execute("UPDATE jobs SET status='running',updated_at=? WHERE job_id=?",(stamp(),jid))
            while True:
                remaining=limits['run_budget_seconds']-(time.monotonic()-started)
                if remaining<=0: break
                row=db.execute("SELECT i.*,f.source_id,f.relative_path FROM job_items i JOIN files f ON f.file_id=i.file_id WHERE i.job_id=? AND i.status='pending' ORDER BY i.item_id LIMIT 1",(jid,)).fetchone()
                if row is None:
                    final='completed_with_errors' if db.execute("SELECT 1 FROM job_items WHERE job_id=? AND status='failed' LIMIT 1",(jid,)).fetchone() else 'completed'
                    break
                item=dict(row)
                if item['attempts']>=limits['max_attempts']:
                    with db: fail(db,item,opts,{'code':'attempt_limit','message':'retry budget exhausted'},limits['max_attempts'])
                    continue
                item['attempts']+=1; item['total_attempts']+=1
                with db: db.execute("UPDATE job_items SET status='running',attempts=?,total_attempts=? WHERE item_id=?",(item['attempts'],item['total_attempts'],item['item_id']))
                file_start=time.monotonic()
                req={'operation':'hash_probe','root':config.sources[item['source_id']]['root'],'relative_path':item['relative_path']}
                response=worker_fn(req,min(remaining,limits['file_timeout_seconds']))
                if not response['ok']:
                    with db:
                        exact.current_state(db,item['file_id'],'unavailable')
                        fail(db,item,opts,response['error'],limits['max_attempts'])
                    continue
                live=response['result']; size=live['fs']['size']
                old=db.execute('SELECT v.*,c.status AS current_status FROM visual_current c JOIN visual_observations v ON v.visual_id=c.visual_id WHERE c.file_id=?',(item['file_id'],)).fetchone()
                if old is None:
                    old=db.execute("SELECT v.*,'unsupported' AS current_status FROM visual_observations v WHERE file_id=? AND status='unsupported' ORDER BY visual_id DESC LIMIT 1",(item['file_id'],)).fetchone()
                same_identity=not old or not old['before_json'] or json.loads(old['before_json']).get('identity')==live['identity']
                binary=db.execute("SELECT h.* FROM active_exact_hashes h WHERE file_id=?",(item['file_id'],)).fetchone()
                with db:
                    if binary and (binary['fingerprint']!=live['fingerprint'] or not same_identity):
                        exact.current_state(db,item['file_id'],'stale'); binary=None
                    state(db,item['file_id'],'verifying')
                # Unsupported containers remain unanalysed, but do not repeatedly reread unchanged bytes.
                if old and old['status']=='unsupported' and old['pipeline_key']==opts['pipeline_key'] and same_identity and not opts['force']:
                    before=json.loads(old['before_json'] or '{}')
                    from .metadata import fingerprint as stat_fingerprint
                    if before.get('fs') and stat_fingerprint(before['fs'])==live['fingerprint']:
                        with db:
                            state(db,item['file_id'],'unsupported',old['visual_id'])
                            db.execute("UPDATE job_items SET status='unsupported',last_error_json=? WHERE item_id=?",(old['error_json'],item['item_id']))
                            db.execute('UPDATE jobs SET cache_saved_bytes=cache_saved_bytes+? WHERE job_id=?',(size,jid))
                        continue
                valid_old=old and old['current_status']!='stale' and old['status']=='valid' and old['fingerprint']==live['fingerprint'] and same_identity and old['pipeline_key']==opts['pipeline_key']
                if valid_old and binary:
                    valid_old=json.loads(old['result_json'])['file_sha256_from_read']==binary['digest']
                if valid_old and old['binary_hash_id'] is not None:
                    valid_old=binary is not None and binary['hash_id']==old['binary_hash_id']
                if valid_old and not opts['force']:
                    with db:
                        state(db,item['file_id'],'valid',old['visual_id'])
                        db.execute("UPDATE job_items SET status='skipped',last_error_json=NULL WHERE item_id=?",(item['item_id'],))
                        db.execute('UPDATE jobs SET cache_saved_bytes=cache_saved_bytes+? WHERE job_id=?',(size,jid))
                    continue
                # Reuse only a binary-equal representative already live-validated in this job.
                rep=None
                if binary and not opts['force']:
                    rep=db.execute('''SELECT v.* FROM active_visual_fingerprints v JOIN active_exact_hashes h ON h.file_id=v.file_id
                     JOIN job_items i ON i.file_id=v.file_id AND i.job_id=? AND i.status IN ('done','skipped','reused_binary')
                     WHERE h.digest=? AND h.size=? AND v.pipeline_key=? AND v.fingerprint=h.fingerprint AND v.file_id!=? LIMIT 1''',
                     (jid,binary['digest'],size,opts['pipeline_key'],item['file_id'])).fetchone()
                if rep:
                    r=json.loads(rep['result_json']); r={**r,'decode_seconds':0,'fingerprint_seconds':0,'peak_rss_bytes':0,
                         'reused_from_visual_id':rep['visual_id'],'orientation_original':None,'orientation_action':'inherited_via_valid_binary_hash'}
                    with db:
                        save(db,item,opts,{'result':r,'fingerprint':live['fingerprint'],'before':live,'read_bytes':0},rep['visual_id'],binary['hash_id'])
                        db.execute('UPDATE jobs SET cache_saved_bytes=cache_saved_bytes+? WHERE job_id=?',(size,jid))
                    continue
                if size>opts['max_file_bytes']:
                    with db: fail(db,item,opts,{'code':'file_size_limit','message':'compressed image exceeds per-file limit'},limits['max_attempts'],status='limited')
                    continue
                used=db.execute('SELECT read_budget_used FROM jobs WHERE job_id=?',(jid,)).fetchone()[0]
                # Phase-wide conservative cap across jobs and retries; metadata validation is separately reserved in the report.
                cumulative=db.execute("SELECT coalesce(sum(read_budget_used),0) FROM jobs WHERE kind='visual'").fetchone()[0]
                if used+size>opts['max_read_bytes'] or cumulative+size+opts['external_read_reserve_bytes']>2*1024**3:
                    with db: db.execute("UPDATE job_items SET status='pending',attempts=attempts-1 WHERE item_id=?",(item['item_id'],))
                    final='budget_exhausted'; break
                deadline=min(limits['run_budget_seconds']-(time.monotonic()-started),limits['file_timeout_seconds']-(time.monotonic()-file_start))
                if deadline<=0: break
                config.outputs['cache'].mkdir(parents=True,exist_ok=True)
                fd,progress=tempfile.mkstemp(prefix='hash-visual-',dir=config.outputs['cache']); os.write(fd,b'0'*24); os.close(fd)
                with db:
                    db.execute('UPDATE jobs SET read_budget_used=read_budget_used+? WHERE job_id=?',(size,jid))
                    db.execute('UPDATE job_items SET reservation_bytes=?,progress_path=? WHERE item_id=?',(size,progress,item['item_id']))
                req.update(opts); req.update(operation='visual',expected_fingerprint=live['fingerprint'],expected_identity=live['identity'],read_allowance=size,progress_path=progress)
                response=worker_fn(req,deadline)
                if not response['ok']:
                    current=db.execute('SELECT * FROM job_items WHERE item_id=?',(item['item_id'],)).fetchone()
                    read=exact.recover_attempt(db,config,current)
                    with db: fail(db,item,opts,response['error'],limits['max_attempts'],read,live,'limited' if response['error']['code']=='memory_limit' else 'failed')
                    continue
                r=response['result']; read=r['read_bytes']
                if not 0<=read<=size: raise ValueError('Invalid worker byte accounting')
                with db:
                    db.execute('UPDATE jobs SET read_bytes=read_bytes+?,read_budget_used=read_budget_used-?+? WHERE job_id=?',(read,size,read,jid))
                    db.execute('UPDATE job_items SET read_bytes=read_bytes+?,reservation_bytes=0,progress_path=NULL WHERE item_id=?',(read,item['item_id']))
                    if r['completed']:
                        if binary and binary['digest']!=r['result']['file_sha256_from_read']: exact.current_state(db,item['file_id'],'stale')
                        save(db,item,opts,r)
                    else: fail(db,item,opts,r['error'],limits['max_attempts'],read,r.get('before'),r['status'])
                Path(progress).unlink(missing_ok=True)
                log(config,{'job_id':jid,'file_id':item['file_id'],'status':'visual_'+r['status'],'read_bytes':read})
        except KeyboardInterrupt: final='cancelled'
        finally:
            for old in db.execute('SELECT * FROM job_items WHERE job_id=? AND progress_path IS NOT NULL',(jid,)).fetchall(): recover(db,config,dict(old),opts,limits)
            with db:
                db.execute("UPDATE job_items SET status='pending' WHERE job_id=? AND status='running'",(jid,))
                db.execute('UPDATE jobs SET status=?,updated_at=?,elapsed_seconds=elapsed_seconds+? WHERE job_id=?',(final,stamp(),time.monotonic()-started,jid))
            result=status(db,jid); db.close()
        return result


def scoped_visuals(db,jid):
    job=db.execute("SELECT * FROM jobs WHERE job_id=? AND kind='visual'",(jid,)).fetchone()
    if job is None: raise ValueError('Unknown visual job_id')
    key=json.loads(job['options_json'])['pipeline_key']
    return [dict(r) for r in db.execute('SELECT v.* FROM active_visual_fingerprints v JOIN job_items i ON i.file_id=v.file_id WHERE i.job_id=? AND v.pipeline_key=? ORDER BY v.file_id',(jid,key))]


def pair_evidence(a,b,params):
    if min(a['luma_stddev'],b['luma_stddev'])<params['min_luma_stddev']: return None,'low_information'
    color=max(abs(x-y) for x,y in zip(a['rgb_mean'],b['rgb_mean']))
    if color>params['rgb_mean_max_difference']: return None,'color_guard'
    best=None
    for variant in a['perceptual']:
        angle=variant['rotation']; wa,ha=(a['height'],a['width']) if angle%180 else (a['width'],a['height'])
        ratio=abs((wa/ha)/(b['width']/b['height'])-1)
        if ratio>params['aspect_relative_tolerance']: continue
        target=b['perceptual'][0]
        ph=(int(variant['phash'],16)^int(target['phash'],16)).bit_count()
        dh=(int(variant['dhash'],16)^int(target['dhash'],16)).bit_count()
        evidence={'phash_hamming':ph,'dhash_hamming':dh,'hash_bits':64,'rotation_a_ccw':angle,
                  'aspect_relative_difference':ratio,'rgb_mean_max_difference':color,'a_hashes':variant,'b_hashes':target,
                  'parameters':params,'candidate_only':True}
        if ph<=params['phash_max_hamming'] and dh<=params['dhash_max_hamming']:
            if best is None or (ph,dh)<(best['phash_hamming'],best['dhash_hamming']): best=evidence
    return (best,None) if best else (None,'distance_or_aspect_guard')


def compare(config,jid,params=None):
    params=params or comparison_options()
    with exclusive(config):
        db=connect(config)
        try:
            rows=scoped_visuals(db,jid); cid=uuid.uuid4().hex
            counts=Counter(); relations=[]
            for i,a in enumerate(rows):
                ra=json.loads(a['result_json'])
                for b in rows[i+1:]:
                    rb=json.loads(b['result_json']); counts['pairs_examined']+=1
                    if a['pixel_sha256']==b['pixel_sha256'] and (a['width'],a['height'])==(b['width'],b['height']):
                        kind='pixel_equivalent'; evidence={'pipeline_key':a['pipeline_key'],'pixel_sha256':a['pixel_sha256'],'width':a['width'],'height':a['height'],'definition':'equality of normalized decoded RGBA8 under the recorded pipeline'}
                    else:
                        evidence,reason=pair_evidence(ra,rb,params)
                        if evidence is None: counts[reason]+=1; continue
                        kind='visual_similarity_candidate'
                    counts[kind]+=1
                    relations.append((cid,a['file_id'],b['file_id'],a['visual_id'],b['visual_id'],kind,'evidenced' if kind=='pixel_equivalent' else 'unverified',encode(evidence)))
            with db:
                db.execute('INSERT INTO visual_comparisons VALUES (?,?,?,?,?,?)',(cid,jid,stamp(),encode(params),encode([r['visual_id'] for r in rows]),encode(dict(counts))))
                db.executemany('INSERT INTO visual_relations(comparison_id,a_file_id,b_file_id,a_visual_id,b_visual_id,kind,status,evidence_json) VALUES (?,?,?,?,?,?,?,?)',relations)
            return {'comparison_id':cid,'job_id':jid,'active_fingerprints':len(rows),'counts':dict(counts),'source_content_bytes_read':0,'parameters':params}
        finally: db.close()


def report(config,jid,comparison_id=None):
    db=exact.readonly(config)
    try:
        db.execute('BEGIN'); job=status(db,jid); rows=scoped_visuals(db,jid)
        if comparison_id is None:
            comparison=db.execute('SELECT * FROM visual_comparisons WHERE job_id=? ORDER BY created_at DESC LIMIT 1',(jid,)).fetchone()
        else: comparison=db.execute('SELECT * FROM visual_comparisons WHERE job_id=? AND comparison_id=?',(jid,comparison_id)).fetchone()
        members=[]
        for r in rows:
            v=json.loads(r['result_json'])
            members.append({'file_id':r['file_id'],'visual_id':r['visual_id'],'source_id':r['source_id'],'relative_path':r['relative_path'],
                            'fingerprint':r['fingerprint'],'last_stat_checked_at':r['checked_at'], 'visual':v,
                            'metadata':json.loads(r['metadata_json'] or 'null'), 'metadata_matches_visual_state':r['metadata_fingerprint']==r['fingerprint'],
                            'labels':[x[0] for x in db.execute('SELECT tag FROM labels WHERE file_id=?',(r['file_id'],))]})
        groups=defaultdict(list)
        for m in members: groups[(m['visual']['pixel_sha256'],m['visual']['width'],m['visual']['height'])].append(m['file_id'])
        pixel_groups=[{'pixel_sha256':key[0],'width':key[1],'height':key[2],'file_ids':ids} for key,ids in groups.items() if len(ids)>1]
        relations=[]; invalid=0
        if comparison:
            for row in db.execute('SELECT * FROM active_visual_relations WHERE comparison_id=?',(comparison['comparison_id'],)):
                d=dict(row); d['evidence']=json.loads(d.pop('evidence_json')); relations.append(d)
            invalid=db.execute('SELECT count(*) FROM visual_relations WHERE comparison_id=?',(comparison['comparison_id'],)).fetchone()[0]-len(relations)
        binary_groups=defaultdict(list)
        for row in db.execute("SELECT h.* FROM active_exact_hashes h JOIN job_items i ON i.file_id=h.file_id WHERE i.job_id=?",(jid,)):
            binary_groups[(row['algorithm'],row['digest'],row['size'])].append({
                'file_id':row['file_id'],'source_id':row['source_id'],'relative_path':row['relative_path'],
                'hash_id':row['hash_id'],'last_stat_checked_at':row['checked_at']})
        binaries=[{'relation_type':'binary_duplicate','algorithm':key[0],'sha256':key[1],'file_size':key[2],
                   'member_count':len(group),'members':group} for key,group in binary_groups.items() if len(group)>1]
        failed=[dict(r) for r in db.execute("SELECT i.file_id,f.source_id,f.relative_path,i.status,i.last_error_json FROM job_items i JOIN files f ON f.file_id=i.file_id WHERE i.job_id=? AND i.status IN ('failed','unsupported','limited','pending')",(jid,))]
        return {'job':job,'comparison':dict(comparison) if comparison else None,'members':members,'unanalysed':failed,
                'pixel_equivalent_group_count':len(pixel_groups),'pixel_equivalent_groups':pixel_groups,
                'visual_candidate_count':sum(r['kind']=='visual_similarity_candidate' for r in relations),'relations':relations,
                'invalidated_historical_relations':invalid,'relation_types':['binary_duplicate','pixel_equivalent','visual_similarity_candidate','related_asset'],
                'binary_duplicates':binaries,
                'related_assets':[{'relation_type':'related_asset',**dict(r)} for r in db.execute('SELECT * FROM candidate_relations WHERE a_file_id IN (SELECT file_id FROM job_items WHERE job_id=?)',(jid,))],
                'note':'Candidate distances are not probabilities. No deletion or preferred original chosen. Valid as of recorded observations; no filesystem monitoring. Hidden same-size/time content replacement can evade stat cache; use bounded --force.'}
    finally: db.close()


def export_report(config,jid):
    from .cli import atomic_text
    result=report(config,jid); base=config.outputs['reports']/('similarity-'+jid)
    atomic_text(base.with_suffix('.json'),lambda f:f.write(encode(result)+'\n'))
    atomic_text(base.with_suffix('.jsonl'),lambda f:f.writelines(encode(r)+'\n' for r in result['relations']))
    def table(f):
        w=csv.writer(f); w.writerow(['kind','a_file_id','b_file_id','status','phash_hamming','dhash_hamming','evidence_json'])
        for r in result['relations']:
            e=r['evidence'];w.writerow([r['kind'],r['a_file_id'],r['b_file_id'],r['status'],e.get('phash_hamming'),e.get('dhash_hamming'),encode(e)])
    atomic_text(base.with_suffix('.csv'),table)
    return {'outputs':[str(base.with_suffix(s)) for s in ('.json','.jsonl','.csv')], 'visual_candidate_count':result['visual_candidate_count'],'pixel_equivalent_group_count':result['pixel_equivalent_group_count']}
