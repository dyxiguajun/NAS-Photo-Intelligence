#!/usr/bin/env python3
"""Sequential local-only RSS diagnosis; no NAS or production DB/config access."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
# Establish src import before the helper, without loading any NAS configuration.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from worker_probe import controlled_worker


def lifecycle():
    results=[]
    for mode,mib,timeout in [('normal',8,10),('crash',0,10),('ignore_term',8,.9)]:
        response,trace=controlled_worker(mode=mode,allocation_mib=mib,timeout=timeout)
        expected=('normal'==mode and response.get('ok') is True
                  or mode=='crash' and response.get('error',{}).get('code')=='worker_crash'
                  or mode=='ignore_term' and response.get('error',{}).get('code')=='timeout')
        trace.update(response=response,passed=bool(expected and all(w['reaped'] for w in trace['workers'])))
        results.append(trace)
    return results


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--iterations',type=int,default=20)
    args=parser.parse_args()
    if not 1<=args.iterations<=30:parser.error('iterations must be 1..30')
    root=Path(__file__).resolve().parents[1];trials=[]
    for number in range(args.iterations):
        response,trace=controlled_worker()
        trace.update(trial=number+1,response=response)
        trace['passed']=(not response['ok'] and response['error']['code']=='memory_limit'
                         and trace['sampled_max_RSS_KiB']>128*1024
                         and all(w['reaped'] for w in trace['workers'])
                         and {s['pgid'] for s in trace['signals']}=={w['pid'] for w in trace['workers']})
        trials.append(trace)
        (root/'reports/phase3_1-rss-diagnostic.json').write_text(json.dumps({'kind':'local controlled sequential memory tests','limit_MiB':128,'logical_allocation_MiB':200,'passed':sum(t['passed'] for t in trials),'trials':trials},indent=2)+'\n')
        print(json.dumps({'trial':number+1,'passed':trace['passed'],'sampled_max_RSS_KiB':trace['sampled_max_RSS_KiB'],'samples':len(trace['samples']),'seconds':trace['elapsed_seconds'],'response':response}),flush=True)
        if not trace['passed']:raise SystemExit(1)
    checks=lifecycle()
    (root/'reports/phase3_1-lifecycle.json').write_text(json.dumps(checks,indent=2)+'\n')
    if not all(c['passed'] for c in checks):raise SystemExit(1)

if __name__=='__main__':main()
