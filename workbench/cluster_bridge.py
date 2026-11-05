"""Offline workbench adapter for the persistent pull-based cluster.

The HTTPS transport runs separately. This process retains its PrivateNetwork
policy and is the only authority publishing the human-facing examination.
"""
from pathlib import Path
import hashlib,json,math,os,sys,threading,time,uuid

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def digest(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def logical(job,revision,role):return hashlib.sha256(f'{job}:{revision}:{role}'.encode()).hexdigest()[:32]

class ClusterBridge:
    def __init__(self,app,config):
        self.app=app;self.config=config;self.root=app.ROOT
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        from cluster.api import Coordinator
        self.database=(self.root/config.get('database','../cluster-state/cluster.sqlite3')).resolve()
        self.artifacts=(self.root/config.get('artifacts','../cluster-state/artifacts')).resolve()
        for path in [self.database,self.artifacts]:
            if path.is_relative_to(self.root/'jobs') or path.is_relative_to(self.root/'cache'):raise ValueError('集群状态必须位于检查和缓存目录之外')
        self.coordinator=Coordinator(self.database,self.artifacts)
        with self.coordinator.store.transaction() as db:
            for key in ('enabled','local_fallback'):
                value=self.config.get(key,False)
                if not isinstance(value,bool):raise ValueError('节点调度设置无效')
                db.execute('INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)',(key,json.dumps(value),time.time()))
        self.stop_event=threading.Event();self.thread=None;self.last_sync=0;self.publisher_id=uuid.uuid4().hex;self.unavailable_since={}
    @property
    def settings(self):
        value=self.coordinator.get_settings()
        return {key:value[key] for key in ('enabled','local_fallback')}
    def configure(self,value):
        if not isinstance(value,dict) or set(value)-{'enabled','local_fallback'} or any(not isinstance(v,bool) for v in value.values()):raise ValueError('节点调度设置无效')
        updated={**self.settings,**value}
        if updated['enabled'] and not (self.root/self.config.get('engine_manifest','')).is_file():
            raise ValueError('节点计算资源尚未就绪')
        self.coordinator.update_settings(updated)
        return updated
    def active(self):return set(self.coordinator.active_job_ids())
    def recoverable(self,job):
        status=self.app.read(job/'status.json')
        if status.get('state')!='running' or not status.get('cluster_managed'):return False
        snapshot=self.snapshot(job)
        return bool(snapshot and snapshot['job_revision']==status.get('cluster_revision') and job.name in self.active())
    def snapshot(self,job):
        from cluster.protocol import ProtocolError
        try:return self.coordinator.job_snapshot(job.name)
        except ProtocolError as error:
            if error.status==404:return None
            raise
    def cancel(self,job):
        from cluster.protocol import ProtocolError
        try:self.coordinator.cancel_job(job.name)
        except ProtocolError as error:
            if error.status!=404:raise
    def deleted(self,ids):
        from cluster.protocol import ProtocolError
        for sid in ids:
            try:self.coordinator.tombstone_job(sid)
            except ProtocolError as error:
                if error.status!=404:raise
    def plan(self,job,status,revision):
        from structures import resolve,inference_plan,LOBE_NAMES
        from progressive import structure_dependencies
        selected,_=resolve(status);mode=status['mode'];output=job/'output'
        manifest_path=(self.root/self.config['engine_manifest']).resolve()
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        engine_id=manifest['engine_id'];engine_sha=digest(manifest_path)
        models=manifest['models']
        geometry=self.app.read(output/'geometry.json');geometry_sha=hashlib.sha256(canonical(geometry)).hexdigest()
        inputs=[]
        for role,filename,kind in [('ct','CT.nii.gz','nifti_ct'),('native_ct','CT_original.nii.gz','nifti_ct'),('geometry','geometry.json','geometry_json'),('native_instances','native_instances.json','instances_json')]:
            path=output/filename
            if not path.is_file():raise ValueError('统一重建输入尚未准备完整')
            item={'id':logical(job.name,revision,role),'role':role,'type':kind,'bytes':path.stat().st_size,'sha256':digest(path),'source_path':str(path)}
            if kind=='nifti_ct':item['geometry']=geometry.get('original_geometry',geometry) if role=='native_ct' else geometry
            inputs.append(item)
        if 'suspects' in selected and (job/'cache/cluster-prior-inputs.json').exists():
            for row in self.app.read(job/'cache/cluster-prior-inputs.json'):
                path=job/row['relative_path']
                if path.is_symlink() or not path.resolve().is_relative_to(job.resolve()):raise ValueError('候选输入路径无效')
                item={'id':logical(job.name,revision,row['role']),'role':row['role'],'type':row['type'],'bytes':path.stat().st_size,'sha256':digest(path),'source_path':str(path)}
                if 'geometry' in row:item['geometry']=row['geometry']
                inputs.append(item)
        input_sha=hashlib.sha256(canonical([{k:row[k] for k in ('id','type','bytes','sha256','role')} for row in sorted(inputs,key=lambda r:(r['role'],r['id']))])).hexdigest()
        budget=self.config.get('stage_memory_bytes',3*1024**3)
        if isinstance(budget,bool) or not isinstance(budget,int) or budget<512*1024**2:raise ValueError('阶段预算无效')
        threads=1
        resources={'threads':threads,'memory_bytes':budget,'cpu_percent':50}
        stages=[];plan=inference_plan(selected,mode)
        input_ids=[x['id'] for x in inputs if x['role'] in {'ct','geometry'}]
        native_input_ids=[x['id'] for x in inputs if x['role']=='native_ct']
        prior_input_ids=[x['id'] for x in inputs if x['role']=='candidate_review' or x['role'].startswith('candidate_mask.')]
        def required_models(key):
            datasets={'total':297,'heart_highres':293,'aorta_highres':293,'lung_highres':291,'trachea_highres':291,'ribs_highres':295,'spine_highres':292,'scapulae_highres':294,'lung_vessels':117,'lung_nodules':913}
            required={298,datasets[key]};rows=[]
            import re
            for dataset in sorted(required):
                matches=[m for m in manifest['models'] if re.match(r'^Dataset'+str(dataset)+r'_',m.get('name',''))]
                if len(matches)!=1:raise ValueError('固定引擎包缺少阶段所需的唯一模型')
                rows.append({k:matches[0][k] for k in ('id','sha256')})
            return sorted(rows,key=lambda m:m['id'])
        for task,key,args,title,percent in plan:
            stages.append({'stage_key':key,'action':'infer_stage','mode':mode,'structures':selected,'parameters':{'task_key':key,'thread_policy':'node_budget'},'resources':resources,'dependencies':[],'input_ids':input_ids,'model_ids':required_models(key)})
        deps=structure_dependencies(mode)
        for structure in selected:
            if structure not in deps:continue
            if structure=='margin':continue  # generated inside canonical nodule export
            stage_structures=[structure]+(['margin'] if structure=='nodules' and 'margin' in selected else [])
            memory=budget
            if structure=='ribs':memory=max(memory,math.prod(geometry['original_geometry']['dimensions_xyz'])*32)
            stages.append({'stage_key':'mesh_'+structure,'action':'mesh_stage','mode':mode,'structures':stage_structures,'parameters':{'margin_mm':status.get('margin_mm'),'thread_policy':'node_budget'},'resources':{**resources,'memory_bytes':memory},'dependencies':sorted(deps[structure]),'input_ids':input_ids+(native_input_ids if structure=='ribs' else []),'model_ids':[]})
        if 'suspects' in selected:
            required={key for _,key,args,_,_ in plan if key=='lung_vessels' or set(args.get('roi_subset',[]))&set(LOBE_NAMES)}
            required.update(s['stage_key'] for s in stages if s['action']=='mesh_stage')
            stages.append({'stage_key':'candidate_suspects','action':'candidate_stage','mode':mode,'structures':['suspects'],'parameters':{'candidate_parameters':status.get('candidate_parameters',{}),'manual_points_lps':status.get('manual_points_lps',[]),'selected_candidate_ids':status.get('candidate_ids',[]),'thread_policy':'node_budget'},'resources':resources,'dependencies':sorted(required),'input_ids':input_ids+prior_input_ids,'model_ids':[]})
        return dict(mode=mode,selected=selected,stages=stages,input_manifest=inputs,geometry=geometry,engine_id=engine_id,engine_sha256=engine_sha,model_ids=models,model_set_sha256=manifest['model_set_sha256'],input_sha256=input_sha,geometry_sha256=geometry_sha)
    def can_schedule(self,job,status):
        """Conservative pre-submission check; never starts a local and remote copy."""
        data=self.plan(job,status,1)
        settings=self.coordinator.get_settings()
        allowances=settings.get('node_allowances',{})
        now=time.time()
        nodes=[n for n in self.coordinator.list_nodes()
               if n.get('state')=='ready' and (n.get('last_seen') or 0)>now-30]
        def matches(node,stage):
            caps=node.get('capabilities') or {}; engine=caps.get('engine') or {}
            if engine.get('id')!=data['engine_id'] or engine.get('sha256')!=data['engine_sha256']:return False
            models={(m.get('id'),m.get('sha256')) for m in caps.get('models',[])}
            if not {(m['id'],m['sha256']) for m in stage.get('model_ids',[])} <= models:return False
            res=caps.get('resources') or {}; allowance=allowances.get(node.get('node_id'),{})
            memory=min(res.get('memory_bytes',0),allowance.get('memory_bytes',res.get('memory_bytes',0)))
            threads=min(res.get('threads',0),allowance.get('threads',res.get('threads',0)))
            cpu=min(res.get('cpu_percent',0),allowance.get('cpu_percent',res.get('cpu_percent',0)))
            required=stage.get('minimum_memory_bytes',stage['resources']['memory_bytes'])
            return memory>=required and threads>=1 and cpu>=1
        return all(any(matches(node,stage) for node in nodes) for stage in data['stages'])
    def submit(self,job):
        with self.app.LOCK:
            status=self.app.read(job/'status.json')
            if status['state']!='running':return
            snapshot=self.snapshot(job)
            revision=max(status.get('cluster_revision',0),snapshot.get('job_revision',0) if snapshot else 0)+1
            data=self.plan(job,status,revision)
            # Persist the submission intent before committing the queue. A
            # crash after commit therefore always leaves the exact revision
            # marker needed by startup; a pre-commit crash is rolled back.
            self.app.update(job,cluster_revision=revision,cluster_managed=True)
            try:
                if snapshot:self.coordinator.revise_job(job.name,revision,**data)
                else:self.coordinator.enqueue_job(job.name,revision,**data)
                self.app.write(job/'progress.json',{'stage':'queued','percent':10,'message':'等待满足资源预算的计算节点'})
            except Exception:
                # Ordinary errors must not orphan a committed remote queue
                # while the caller restores the previous local preview.
                self.cancel(job)
                raise
    def admin_get(self):
        return {'settings':self.settings,'nodes':self.coordinator.list_nodes(),'enrollments':[e for e in self.coordinator.list_enrollments() if e['state'] in {'pending','pending_approval','pending_signing','provisioning'}]}
    def admin_post(self,action,data):
        if action=='settings':return self.configure(data)
        if action=='pairing':return self.coordinator.create_pairing_token()
        if action=='approve':return self.coordinator.approve_enrollment(data['enrollment_id'])
        if action=='revoke':return self.coordinator.revoke_node(data['node_id'])
        if action in {'disable','enable'}:return self.coordinator.set_node_enabled(data['node_id'],action=='enable')
        raise ValueError('未知节点管理操作')
    def start(self):
        # An OS-held lock fences publishers across process restarts/duplicates.
        self.publisher_lock=(self.database.parent/'publisher.lock').open('a+b')
        if os.name=='nt':
            import msvcrt
            self.publisher_lock.seek(0);self.publisher_lock.write(b'1');self.publisher_lock.flush();self.publisher_lock.seek(0)
            msvcrt.locking(self.publisher_lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(self.publisher_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.thread=threading.Thread(target=self.loop,name='cluster-single-publisher',daemon=False);self.thread.start()
    def close(self):
        self.stop_event.set()
        if self.thread:self.thread.join()
        self.coordinator.release_publisher(self.publisher_id)
        self.coordinator.close()
        if getattr(self,'publisher_lock',None):self.publisher_lock.close()
    def loop(self):
        while not self.stop_event.wait(.5):
            try:self.tick()
            except Exception:
                # Keep the durable outbox retryable. No patient or exception
                # content is emitted to public transport logs.
                self.stop_event.wait(2)
    def tick(self):
        if time.monotonic()-getattr(self,'last_sync',0)>2:
            self.sync_progress();self.last_sync=time.monotonic()
        event=self.coordinator.claim_publish_event(publisher_id=self.publisher_id)
        if not event:return
        job=self.app.JOBS/event['job_id']
        try:
            with self.app.LOCK:
                if not (job/'status.json').exists():
                    self.coordinator.tombstone_job(event['job_id']);return
                status=self.app.read(job/'status.json')
                if status['state']!='running' or status.get('cluster_revision')!=event['job_revision']:
                    self.coordinator.ack_publish_event(event['event_id'],publisher_id=self.publisher_id);return
                folder=job/'cache/cluster-events';folder.mkdir(parents=True,exist_ok=True)
                self.app.write(folder/(event['event_id']+'.json'),event)
            code,error=self.app.execute_worker(job,'cluster_apply',extra_env={'WORKBENCH_CLUSTER_EVENT_ID':event['event_id']})
            with self.app.LOCK:
                current=self.app.read(job/'status.json')
                if current['state']=='cancelled':
                    self.app.finish_update(job,False,'本次更新已取消，保留上一版结果')
                elif code:
                    raise RuntimeError(error or '节点结果发布失败')
                elif event.get('job_complete'):
                    if not self.app.finish_update(job,True):self.app.update(job,state='completed',finished_at=time.time(),error=None)
                self.coordinator.ack_publish_event(event['event_id'],publisher_id=self.publisher_id)
        except Exception:
            self.coordinator.release_publish_event(event['event_id'],publisher_id=self.publisher_id)
            with self.app.LOCK:
                if (job/'status.json').exists():
                    status=self.app.read(job/'status.json')
                    if status['state']=='running' and status.get('cluster_revision')==event['job_revision']:
                        count=status.get('cluster_publish_failures',0)+1
                        self.app.update(job,cluster_publish_failures=count)
                        if count>=3:
                            self.coordinator.cancel_job(job.name)
                            if not self.app.finish_update(job,False,'节点结果发布失败，已保留上一版预览'):
                                self.app.update(job,state='failed',error='节点结果发布失败，输入已保留')
            raise

    def sync_progress(self):
        self.coordinator.reap_expired()
        for path in self.app.JOBS.glob('*/status.json'):
            with self.app.LOCK:
                status=self.app.read(path)
                if status.get('state')!='running' or not status.get('cluster_managed'):
                    self.unavailable_since.pop(path.parent.name,None)
                    continue
                job=path.parent;snapshot=self.snapshot(job)
                if not snapshot or snapshot.get('job_revision',snapshot.get('revision'))!=status.get('cluster_revision'):continue
                if snapshot['state'] in {'failed','cancelled','tombstoned'}:
                    if not self.app.finish_update(job,False,'节点任务未完成，已保留上一版预览'):
                        self.app.update(job,state='failed',error='节点任务未完成，请查看节点状态后重试')
                    continue
                stages=snapshot['stages'];done=sum(s['status']=='completed' for s in stages)
                active={l['node_id'] for l in snapshot['leases'] if l['state']=='active'}
                if active:
                    self.unavailable_since.pop(job.name,None)
                elif self.settings['local_fallback'] and done<len(stages):
                    since=self.unavailable_since.setdefault(job.name,time.monotonic())
                    if time.monotonic()-since>=60:
                        self.unavailable_since.pop(job.name,None)
                        if not self.can_schedule(job,status):
                            # Cancel and fence all remote leases before requeueing locally.
                            # Even a simultaneous claim cannot publish after cancellation.
                            self.cancel(job)
                            self.app.update(job,state='queued',cluster_managed=False,cluster_force_local=True,error=None)
                            self.app.write(job/'progress.json',{'stage':'queued','percent':0,'message':'节点不可用，转为服务器计算','updated_at':time.time()})
                            self.app.QUEUE.put((job,'run'))
                            continue
                if active:message=f'已完成 {done} / {len(stages)} 个阶段，{len(active)} 个节点正在处理'
                elif done==len(stages):message='正在校验并发布重建结果'
                else:
                    nodes=self.coordinator.list_nodes()
                    ready=[n for n in nodes if n.get('state',n.get('status'))=='ready' and (n.get('last_seen') or 0)>time.time()-30]
                    message='等待满足内存、磁盘和引擎版本要求的节点' if ready else '等待计算节点连接或恢复参与'
                previous=self.app.read(job/'progress.json') if (job/'progress.json').exists() else {}
                if previous.get('message')!=message:
                    self.app.write(job/'progress.json',{'stage':'cluster','percent':10+int(75*done/max(1,len(stages))),'message':message,'updated_at':time.time()})
