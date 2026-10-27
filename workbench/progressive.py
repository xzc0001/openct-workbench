"""One native CT publication, append-only immutable mesh assets per reconstruction."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import time
import uuid

TOKEN = re.compile('[a-f0-9]{32}')

def reconstruction_plan(selected):
    from structures import LABELS
    return [{'id':sid,'group':'ribcage' if sid=='ribs' else sid,'label':LABELS[sid],'phase':'waiting','model_ids':[]} for sid in selected if sid in LABELS and sid not in {'suspects','segments','fractures'}]

def structure_dependencies(mode):
    def key(full):return full if mode=='full' else 'total'
    return {'heart':{key('heart_highres')},'aorta':{key('aorta_highres')},'lungs':{key('lung_highres')},'lobes':{key('lung_highres')},'airways':{key('trachea_highres'),'lung_vessels'},'arteries':{'lung_vessels'},'veins':{'lung_vessels'},'nodules':{'lung_nodules'},'margin':{'lung_nodules'},'scapulae':{key('scapulae_highres')},'ribs':{'ribs_highres','spine_highres','scapulae_highres'} if mode=='full' else {'total'}}

def set_phase(output, phase, structures=(), task=None, mode=None, label=None):
    folder=current(output)
    if folder is None:return
    record=json.loads((folder/'snapshot.json').read_text(encoding='utf-8'))
    dependencies=structure_dependencies(mode) if task else {}
    for item in record.get('plan',[]):
        if item['phase'] in {'ready','empty'}:continue
        if task:
            item['phase']='segmenting' if task in dependencies.get(item['id'],set()) else 'waiting'
        elif item['id'] in structures:item['phase']=phase
    record['revision']+=1
    record['activity']={'task':task,'phase':phase,'label':label} if task else None
    save(folder/'snapshot.json',record)

def presentation_plan(snapshot, status):
    """Terminal task states stop pending placeholders, including rollback updates."""
    plan=[dict(item) for item in snapshot.get('plan',reconstruction_plan(status.get('structures',[])))]
    for item in plan:
        item['model_ids']=[m['id'] for m in snapshot.get('models',[]) if m['group']==item['group']]
        if snapshot.get('complete'):
            item['phase']='ready' if item['model_ids'] else 'empty'
        elif status.get('state') not in {'queued','running'} and item['phase'] not in {'ready','empty'}:
            item['phase']='interrupted' if status.get('error') else 'stopped'
    return plan

def save(path, value):
    pending=path.with_name('.'+uuid.uuid4().hex+'.tmp')
    pending.write_text(json.dumps(value,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    for attempt in range(20):
        try:os.replace(pending,path);return
        except PermissionError:
            if attempt==19:raise
            time.sleep(.05)

def current(output):
    pointer=Path(output)/'live/current.json'
    if not pointer.is_file():return None
    token=json.loads(pointer.read_text(encoding='utf-8'))['token']
    if not TOKEN.fullmatch(token):raise ValueError('Invalid progressive preview')
    folder=Path(output)/'live'/token
    if folder.is_symlink():raise ValueError('Invalid progressive preview directory')
    return folder

def initialize(job, canonical, geometry, notify=lambda message:None):
    from viewer_pack import pack,original_volume
    import pydicom
    output=job/'output'; token=uuid.uuid4().hex
    folder=output/'live'/token;folder.mkdir(parents=True)
    base=f'/results/{job.name}/live/{token}'
    status=json.loads((job/'status.json').read_text(encoding='utf-8'))
    instances=output/'native_instances.json'
    if instances.is_file():numbers=json.loads(instances.read_text(encoding='utf-8'))
    else:
        series=next(s for s in json.loads((job/'inventory.json').read_text(encoding='utf-8'))['series'] if s['id']==status['series_id'])
        numbers=[int(getattr(pydicom.dcmread(p,stop_before_pixels=True,specific_tags=['InstanceNumber']),'InstanceNumber',i+1)) for i,p in enumerate(series['files'])]
        save(instances,numbers)
    plan=reconstruction_plan(status.get('structures',[]))
    scene={'models':[],'nodules':[],'coordinate_system':'RAS','units':'mm','source_geometry':geometry,'preferred_render_mode':'surface','method_summary':'重建结果逐项生成，自动分割待影像复核。','stream':{'snapshot':base+'/snapshot','token':token,'plan':plan}}
    native,original=original_volume(job,canonical)
    pack(folder,canonical,scene,[],native,original,numbers,notify,online_base=base+'/view')
    save(folder/'snapshot.json',{'revision':0,'models':[],'masks':[],'nodules':[],'complete':False,'plan':plan})
    save(output/'live/current.json',{'token':token,'started_at':status.get('started_at')})
    # Only one CT copy is retained for progressive viewing. Final offline export
    # remains separate; a superseded live tab can reopen the current preview.
    for old in (output/'live').iterdir():
        if old.is_dir() and TOKEN.fullmatch(old.name) and old.name!=token:
            if old.is_symlink():raise ValueError('Invalid progressive preview directory')
            shutil.rmtree(old)
    return folder

def publish(output, models, masks, nodules=()):
    folder=current(output)
    if folder is None:return
    from mesh_lod import display_lods
    from preview_package import Assets,mesh_assets
    base=f'/results/{Path(output).parent.name}/live/{folder.name}'
    class MeshAssets(Assets):
        def add(self,compressed,raw_bytes):
            if len(compressed)<18 or compressed[:2]!=b'\x1f\x8b' or int.from_bytes(compressed[-4:],'little')!=raw_bytes%2**32:
                raise ValueError('Invalid progressive geometry')
            key=hashlib.sha256(compressed).hexdigest();path=folder/'mesh'/key
            path.parent.mkdir(exist_ok=True)
            if not path.exists():
                pending=path.with_suffix('.tmp');pending.write_bytes(compressed);os.replace(pending,path)
            return {'asset':key,'bytes':raw_bytes,'url':base+'/asset/'+key+'.gz'}
    assets=MeshAssets(None)
    record=json.loads((folder/'snapshot.json').read_text(encoding='utf-8'))
    by_id={m['id']:m for m in record['models']}
    for model in models:
        # Geometry is immutable within a run. Metadata may be enriched later.
        if model['id'] in by_id:
            saved=by_id[model['id']]
            saved.update({k:v for k,v in model.items() if k not in {'positions','indices','normals','lod','arrays','encoding'}})
        else:
            lod=display_lods({'models':[model]})['models'][0]
            by_id[model['id']]=mesh_assets(lod,assets)
    mask_by_id={m['id']:m for m in record['masks']}
    for mask in masks:
        if mask and mask['id'] not in mask_by_id:
            from math import prod
            count=(prod(mask['size'])+7)//8
            mask_by_id[mask['id']]={**mask,'data':assets.encoded(mask['data'],count)}
    candidates={m['id']:m for m in record['nodules']}
    candidates.update({m['id']:m for m in nodules})
    record.update(revision=record['revision']+1,models=list(by_id.values()),masks=list(mask_by_id.values()),nodules=list(candidates.values()))
    save(folder/'snapshot.json',record)

def finish(output):
    folder=current(output)
    if folder:
        record=json.loads((folder/'snapshot.json').read_text(encoding='utf-8'))
        record.update(complete=True,revision=record['revision']+1)
        record['plan']=presentation_plan(record,{'state':'completed'})
        save(folder/'snapshot.json',record)
        shutil.rmtree(folder/'exports',ignore_errors=True)

def cache_export(output, selected, models, masks, nodules, missing):
    folder=current(output)
    if folder is None:return
    cache=folder/'exports';cache.mkdir(exist_ok=True)
    key=hashlib.sha256(json.dumps(selected).encode()).hexdigest()
    save(cache/(key+'.json'),{'selected':selected,'models':models,'masks':masks,'nodules':nodules,'missing':missing})
    record=json.loads((folder/'snapshot.json').read_text(encoding='utf-8'))
    for item in record.get('plan',[]):
        if item['id'] in selected:
            item['model_ids']=[m['id'] for m in record['models'] if m['group']==item['group']]
            item['phase']='ready' if item['model_ids'] else 'empty'
    record['revision']+=1
    record['activity']=None
    save(folder/'snapshot.json',record)

def cached_export(output):
    """Only the current unique run can reuse completed, exact surface exports."""
    folder=current(output)
    rows=[] if folder is None else [json.loads(p.read_text(encoding='utf-8')) for p in sorted((folder/'exports').glob('*.json'))]
    return {key:[item for row in rows for item in row[key]] for key in ['selected','models','masks','nodules','missing']}

def ready_structures(selected, mode, completed):
    dependencies=structure_dependencies(mode)
    return [s for s in selected if s in dependencies and dependencies[s].issubset(completed)]
