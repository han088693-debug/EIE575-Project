"""Resume the explicitly approved d25 budget revision, one real MCP request per fixed seed."""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import calibrate
import calibration_driver as driver
import experiment as ex

PROJECT=Path(__file__).resolve().parents[1]
OUTPUT=PROJECT/'results/calibration/core_v1_budget2'
PARENT=PROJECT/'results/calibration/core_v1'
CLOSED=OUTPUT/'v1_closed'
SNAPSHOT_SHA='e11f2673af1005c75a267841338ec0f6287abef8d8b14196bb39e98525e9f550'
IDS={100:'e3e46441f32b6caa03',101:'e5e0f603c259082a01'}
ALLOWANCE=5400


def now():return datetime.now(timezone.utc).isoformat()


def require(ok,reason):
    if not ok:raise ValueError(reason)


def prepare(cfg):
    require(ex.file_hash(CLOSED/'snapshot.json')==SNAPSHOT_SHA,'Closed E0 snapshot fingerprint changed')
    base=ex.json_read(CLOSED/'index.json');snap=ex.json_read(CLOSED/'snapshot.json')
    require(ex.file_hash(PARENT/'index.json')==snap['files']['index.json'],'Parent index was edited')
    version,reason=driver.campaign_version(PROJECT,cfg)
    require(not reason and version==base['version'],'Scientific campaign identity changed')
    instance=hashlib.sha256(str(PROJECT).casefold().encode()).hexdigest()[:12]
    tmp=Path(tempfile.gettempdir());main=tmp/('tabr_formal_'+instance)
    queues={100:main,101:tmp/('tabr_peer_'+instance+'_'+IDS[101])}
    index=OUTPUT/'index.json';records=[]
    base_registry=PROJECT/'configs/formal_probes.json';folder=PROJECT/'configs/p3'
    with ex.locked(folder):
        catalog=ex.json_read(base_registry);original=deepcopy(catalog)
        for seed,request_id in IDS.items():
            chain=[q for q in base['requests'] if q['job']==[25,20,seed]]
            expected='e'+ex.identity([version['key'],25,20,seed])[:14]+f'a{len(chain)+1:02d}'
            require(request_id==expected and len(chain)==(2 if seed==100 else 0),'Original request chain mismatch')
            canonical=f"p3e0_{version['key'][:14]}_d25_w20_s{seed}"
            alias='b2'+ex.identity([canonical,request_id,ALLOWANCE])[:12]
            legacy=catalog['probes'][canonical];legacyfile=PROJECT/'configs'/legacy['config'];baseline=ex.json_read(legacyfile)
            amended=deepcopy(baseline);amended.setdefault('execution',{})['wall_timeout_s']=ALLOWANCE
            require(ex.physics_fields(baseline)==ex.physics_fields(amended),'Physical configurations differ')
            newfile=legacyfile.with_name(alias+'.json');driver.immutable(newfile,amended)
            spec=dict(legacy,config=newfile.relative_to(PROJECT/'configs').as_posix())
            require(alias not in catalog['probes'] or catalog['probes'][alias]==spec,'Registration collision')
            catalog['probes'][alias]=spec
            own=queues[seed];other=queues[201-seed]
            require(not (other/'requests'/request_id/'state.json').exists(),'Request exists in the wrong queue')
            records.append(dict(job=[25,20,seed],request_id=request_id,probe=alias,original_probe=canonical,
                                queue=str(own),original_config_sha256=ex.file_hash(legacyfile),config_sha256=ex.file_hash(newfile),
                                physics_sha256=ex.identity(ex.physics_fields(baseline)),state='submitting',
                                original_requests=[q['request_id'] for q in chain],
                                registration_sha256=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()+newfile.read_bytes()).hexdigest(),
                                parent_scientific_key=ex.json_read(Path(chain[-1]['attempt'])/'run_manifest.json')['simulation_job_key'] if chain else None))
        if catalog!=original:ex.save(base_registry,catalog)
    approval=dict(scope='E0 engineering',budget_revision=2,parent_snapshot_sha256=SNAPSHOT_SHA,
                  approved_text='允许双槽补齐（推荐）：种子 100 追加一次，种子 101 同时启动，每次最多 90 分钟，科学参数不变。',
                  max_parallel=2,wall_timeout_s=ALLOWANCE,additional_requests=2,science_campaign=version['key'],jobs=records)
    driver.immutable(OUTPUT/'budget_revision.json',approval)
    if index.exists():state=ex.json_read(index)
    else:
        require(all(not (Path(r['queue'])/'requests'/r['request_id']/'state.json').exists() for r in records),'Unindexed previous revision requests')
        for q in base['requests']:
            if q['state']=='accepted':
                src=CLOSED/q['report'];require(ex.file_hash(src)==q['report_sha256'],'Inherited measurement hash mismatch')
                ex.write_atomic(OUTPUT/q['report'],src.read_bytes())
        for src in (CLOSED/'timeout_extensions').glob('*.json'):ex.write_atomic(OUTPUT/'timeout_extensions'/src.name,src.read_bytes())
        ex.write_atomic(OUTPUT/'decision.json',(CLOSED/'decision.json').read_bytes())
        state=deepcopy(base);state.update(status='submitting',budget_revision=2,parent_snapshot_sha256=SNAPSHOT_SHA)
        state['requests'].extend(deepcopy(records));ex.save(index,state)
    require(state['version']==version and state['requests'][:-2]==base['requests'] and
            [q['request_id'] for q in state['requests'][-2:]]==list(IDS.values()),'Revision index diverged')
    return state


@contextmanager
def queue_env(queue):
    key='EIE575_FORMAL_QUEUE';previous=os.environ.get(key);os.environ[key]=str(queue)
    try:yield
    finally:
        if previous is None:os.environ.pop(key,None)
        else:os.environ[key]=previous


def rpc(cfg,row,method):
    payload={'request_id':row['request_id']}
    if method=='start_probe':payload['probe']=row['probe']
    with queue_env(row['queue']):
        for i in range(5):
            try:return driver.protocol(cfg,method,payload)
            except (RuntimeError,subprocess.TimeoutExpired) as exc:
                if i==4 or not (isinstance(exc,subprocess.TimeoutExpired) or any(k in str(exc) for k in ('Permission denied','WinError 32','WinError 5'))):raise
                time.sleep(1+i)


def start(cfg,state):
    for row in state['requests'][-2:]:
        if row['state']!='submitting':continue
        with queue_env(row['queue']):
            host=subprocess.run([cfg['tools']['mcp_python'],'-B','-X','utf8',str(PROJECT/'tools/formal_mcp.py'),'--start-host'],
                                cwd=PROJECT,check=True,capture_output=True,encoding='utf-8',timeout=40)
        require(json.loads(host.stdout)['ready'],'Formal host unavailable')
        reply=rpc(cfg,row,'start_probe')
        require(reply['request_id']==row['request_id'] and reply['registration']==row['registration_sha256'],'MCP registration mismatch')
        row.update(state='submitted',mcp_created_at=reply['created_at']);ex.save(OUTPUT/'index.json',state)
        print(now(),row['job'],row['request_id'],row['queue'],'submitted',flush=True)


def audit(state):
    import psutil
    slots=[]
    for row in state['requests'][-2:]:
        base=PROJECT/'results/probes'/row['probe']/row['request_id'];paths=list(base.rglob('engine_status.json'))
        require(len(paths)<=1,'Unexpected second attempt for an approved request')
        source=Path(row['queue'])/'requests'/row['request_id']/'state.json';mcp=ex.json_read(source)
        item=dict(request=row['request_id'],seed=row['job'][-1],queue=row['queue'],host=mcp.get('owner'),state=mcp['state'])
        if paths:
            eng=ex.json_read(paths[0]);item.update(attempt=paths[0].parent.parent.name,port=eng.get('port'),engine_status=eng['status'])
            trace=paths[0].with_name('resource_trace.csv')
            if trace.is_file():
                with trace.open('rb') as f:
                    fields=f.readline().decode('utf-8-sig').strip().split(',');f.seek(max(0,trace.stat().st_size-1024));lines=f.read().split(b'\n')
                for line in reversed(lines[:-1]):
                    values=line.decode('ascii').strip().split(',')
                    if len(values)==len(fields) and values[0]!=fields[0]:item['resources']=dict(zip(fields,values));break
            descendants=[]
            if mcp['state']=='running' and mcp.get('engine'):
                try:
                    proc=psutil.Process(mcp['engine']['pid'])
                    if abs(proc.create_time()-mcp['engine']['create_time'])<.001:
                        descendants=[dict(pid=c.pid,create_time=c.create_time(),name=c.name()) for c in proc.children(recursive=True)
                                     if c.name().lower() in ('sumo.exe','eie575_tabr_starter.exe')]
                except psutil.NoSuchProcess:pass
            item['solver_processes']=descendants
        slots.append(item)
    if all(item.get('solver_processes') for item in slots):
        require(slots[0]['port']!=slots[1]['port'] and slots[0]['host']!=slots[1]['host'],'Slot ownership conflict')
    with (OUTPUT/'parallel_resources.jsonl').open('a',encoding='utf-8') as f:
        f.write(json.dumps(dict(checked_utc=now(),available_ram=psutil.virtual_memory().available,slots=slots))+'\n')


def advance(cfg,state):
    for row in state['requests'][-2:]:
        if row['state'] in ('accepted','engineering_failed','invalid_measurement'):continue
        status=rpc(cfg,row,'get_status');require(status['request_id']==row['request_id'] and status['registration']==row['registration_sha256'],'Different request')
        phase=status['state']
        if phase in ('queued','running'):
            if row['state']!=phase:row['state']=phase;ex.save(OUTPUT/'index.json',state)
            continue
        result=rpc(cfg,row,'get_results');check=Path(row['queue'])/'requests'/row['request_id']/'state.json'
        require(result['request_id']==row['request_id'] and result['registration']==row['registration_sha256'],'Different result')
        archived=OUTPUT/'receipts'/(row['request_id']+'.json');original=check.read_bytes()
        if archived.exists():require(archived.read_bytes()==original,'Terminal receipt changed')
        else:ex.write_atomic(archived,original)
        row['receipt_sha256']=ex.file_hash(check)
        if phase=='completed':
            require(result['complete'] and result['exit_code']==0,'Missing completed run evidence')
            if row['parent_scientific_key']:require(row['parent_scientific_key']==result['simulation_job_key'],'Parent job key changed')
            folder=Path(result['attempt']);manifest=ex.json_read(folder/'run_manifest.json');snapshot=ex.json_read(folder/'config_snapshot.json')
            require(snapshot['execution']['wall_timeout_s']==ALLOWANCE,'Execution budget mismatch')
            bfile=PROJECT/'results/probes'/row['probe']/row['request_id']/'batch.json';b=ex.json_read(bfile)
            spec=ex.json_read(PROJECT/'configs/formal_probes.json')['probes'][row['original_probe']]
            original_cfg=ex.json_read(PROJECT/'configs'/spec['config'])
            effective=dict(original_cfg,density_per_lane_km=float(spec['density_per_lane_km']))
            require(ex.freeze_physics_sha256(original_cfg)==row['physics_sha256'] and
                    b['physics_sha256']==ex.freeze_physics_sha256(effective) and
                    len(b['attempts'][manifest['job_id']])==1,'Effective physics or attempt count mismatch')
            driver.evaluate_finished(OUTPUT,state,row,result,state['version'])
        else:
            require(phase in driver.FAILURES,'Unexpected MCP phase');row.update(state='engineering_failed',reason=result.get('attempt_error') or result.get('error') or phase,
                    attempt=result.get('attempt'),attempt_id=result.get('attempt_id'));ex.save(OUTPUT/'index.json',state)
        print(now(),row['job'],row['request_id'],row['state'],flush=True)
    pair=state['requests'][-2:]
    if not all(r['state'] in ('accepted','engineering_failed','invalid_measurement') for r in pair):
        state['status']='running';ex.save(OUTPUT/'index.json',state);return False
    if any(r['state']!='accepted' for r in pair):state['status']='engineering_failed'
    else:
        reports=driver.load_reports(OUTPUT,state,state['version'],deep=True)
        geo=sorted((PROJECT/'verification/p2/sumo').glob('geometry_exclusion_d*_seed*/inputs/scenario.json'))
        decision=calibrate.decide(cfg,reports,geometry_exclusions=geo);ex.save(OUTPUT/'decision.json',decision);state['status']=decision['status']
    ex.save(OUTPUT/'index.json',state);print(now(),state['status'],flush=True);return True


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',action='store_true');args=parser.parse_args()
    if not args.run:
        data=ex.json_read(OUTPUT/'index.json') if (OUTPUT/'index.json').is_file() else {};print(data.get('status','unregistered'));return
    with ex.locked(OUTPUT):
        cfg=driver.load_config(PROJECT/'configs/core.json');state=prepare(cfg);start(cfg,state)
        while True:
            try:audit(state)
            except (FileNotFoundError,PermissionError):pass
            if advance(cfg,state):break
            time.sleep(16)


if __name__=='__main__':main()
