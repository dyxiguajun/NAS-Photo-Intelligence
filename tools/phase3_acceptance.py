#!/usr/bin/env python3
"""Read-only bounded final checks, local comparison and report export. No discovery or content reads."""
from contextlib import closing
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from nas_photo import similarity as sim
from nas_photo.config import Config
from nas_photo.process import run_worker


def main():
    config=Config('config/local.json');root=config.project
    before=json.loads((root/'reports/phase3-source-before.json').read_text())+json.loads((root/'reports/phase3-added-before.json').read_text())
    after=[]
    for item in before:
        response=run_worker({'operation':'hash_probe','root':config.sources[item['source_id']]['root'],'relative_path':item['relative_path']},10)
        after.append({'source_id':item['source_id'],'relative_path':item['relative_path'],**response})
    (root/'reports/phase3-source-after.json').write_text(json.dumps(after,ensure_ascii=False,indent=2)+'\n')
    checks=[]
    for old,new in zip(before,after):
        checks.append({'source_id':old['source_id'],'relative_path':old['relative_path'],'unchanged':old['ok'] and new['ok'] and old['result']==new['result']})
    with closing(sqlite3.connect(root/'data/index.sqlite3')) as db,closing(sqlite3.connect(root/'data/index.pre-phase3.sqlite3')) as backup:
        tables=[r[0] for r in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        preserved={}
        for table in tables:
            old=backup.execute('SELECT * FROM '+table).fetchall();new=db.execute('SELECT * FROM '+table).fetchall()
            preserved[table]=all(row in new for row in old)
        metrics={'schema_version':db.execute('PRAGMA user_version').fetchone()[0],
                 'integrity_check':db.execute('PRAGMA integrity_check').fetchone()[0],'foreign_key_check':db.execute('PRAGMA foreign_key_check').fetchall(),
                 'old_rows_preserved':preserved,'indexed_files':db.execute('SELECT count(*) FROM files').fetchone()[0],
                 'metadata_observations':db.execute('SELECT count(*) FROM observations').fetchone()[0],
                 'hash_observations':db.execute('SELECT count(*) FROM hash_observations').fetchone()[0],
                 'visual_read_bytes_observed':db.execute("SELECT coalesce(sum(read_bytes),0) FROM jobs WHERE kind='visual'").fetchone()[0],
                 'visual_read_budget_charged':db.execute("SELECT coalesce(sum(read_budget_used),0) FROM jobs WHERE kind='visual'").fetchone()[0],
                 'no_jobs_running':db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0]==0}
    jid=json.loads((root/'reports/phase3-nas-expanded.log').read_text().splitlines()[0])['job_id']
    with patch('nas_photo.similarity.run_worker',side_effect=AssertionError('threshold comparison cannot read NAS')):
        strict=sim.compare(config,jid,sim.comparison_options(phash=4,dhash=8))
        default=sim.compare(config,jid)
    (root/'reports/phase3-threshold-recomparison.json').write_text(json.dumps({'strict':strict,'default':default},indent=2)+'\n')
    outputs=sim.export_report(config,jid);report=sim.report(config,jid)
    metrics.update({'unique_real_files':len(before),'new_real_files':len(before)-15,'source_state_checks':checks,'original_files_changed':sum(not x['unchanged'] for x in checks),
                    'nas_write_operations_performed':0,'decoded_files':len(report['members']),
                    'decoded_formats':dict(Counter(m['visual']['format'] for m in report['members'])),
                    'unanalysed':report['unanalysed'],'L1_groups':report['pixel_equivalent_group_count'],'L2_candidates':report['visual_candidate_count'],
                    'cross_source_visual_candidates':sum(next(m for m in report['members'] if m['file_id']==r['a_file_id'])['source_id']!=next(m for m in report['members'] if m['file_id']==r['b_file_id'])['source_id'] for r in report['relations'] if r['kind']=='visual_similarity_candidate'),
                    'main_visual_job':report['job'],'outputs':outputs,'phase_source_read_budget_bytes':2*1024**3,
                    'external_metadata_read_reserve_bytes':512*1024**2,
                    'metadata_read_accounting_note':'13 new files total 17,037,215 logical bytes. ExifTool/file reads are not instrumented: 512 MiB conservatively reserved, not an exact measured total or wire-traffic value.',
                    'metadata_read_bytes_exact':None, 'threshold_comparison_source_reads':0})
    metrics['automated_tests']={'log':'reports/phase3-tests.log','passed': 'OK' in (root/'reports/phase3-tests.log').read_text(), 'summary':(root/'reports/phase3-tests.log').read_text().strip().splitlines()[-3:]}
    metrics['dependencies_check']=(root/'reports/phase3-dependencies-check.log').read_text().strip()
    cache=json.loads((root/'reports/phase3-nas-final-cache.log').read_text().splitlines()[-1]);metrics['cache_validation']=cache
    (root/'reports/phase3_validation.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+'\n')
    assert all(preserved.values()) and metrics['integrity_check']=='ok' and not metrics['foreign_key_check']
    assert metrics['original_files_changed']==0 and metrics['unique_real_files']<=100 and metrics['visual_read_budget_charged']+metrics['external_metadata_read_reserve_bytes']<2*1024**3
    assert cache['program_read_bytes_observed']==0 and cache['job']['status']=='completed'
    print(json.dumps({k:metrics[k] for k in ('unique_real_files','new_real_files','decoded_files','decoded_formats','L1_groups','L2_candidates','visual_read_bytes_observed','integrity_check','original_files_changed','old_rows_preserved')},ensure_ascii=False))

if __name__=='__main__':main()
