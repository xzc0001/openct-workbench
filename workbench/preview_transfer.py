"""Transfer only indexed preview data; the receiving server builds its own HTML."""
from pathlib import Path
import hashlib
import json
import math
import re
import shutil
import stat
import os
import time
import uuid
import zipfile
import zlib

MAX_ARCHIVE=4*1024**3
MAX_INDEX=64*1024**2
MAX_ASSET=128*1024**2
MAX_INFLATED=8*1024**3


def export_preview(output, destination, metadata=None):
    output=Path(output);pointer=json.loads((output/'online/current.json').read_text(encoding='utf-8'))
    generation=pointer['generation']
    if not re.fullmatch(r'[a-f0-9]{32}',generation):raise ValueError('预览版本无效')
    folder=output/'online'/generation
    if folder.is_symlink():raise ValueError('预览目录无效')
    with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
        archive.write(folder/'index.json','index.json')
        archive.write(folder/'source.html','source.html')
        archive.writestr('metadata.json',json.dumps(metadata or {},ensure_ascii=False))
    if json.loads((output/'online/current.json').read_text(encoding='utf-8'))!=pointer:
        Path(destination).unlink(missing_ok=True);raise ValueError('预览正在更新，请稍后重试')
    if Path(destination).stat().st_size>MAX_ARCHIVE:
        Path(destination).unlink(missing_ok=True);raise ValueError('预览超出上传大小限制')


def _finite_tree(root):
    stack=[root];remaining=3_000_000
    while stack:
        obj=stack.pop();remaining-=1
        if remaining<0:raise ValueError('预览数据过于复杂')
        if isinstance(obj,dict):
            if any(not isinstance(k,str) or len(k)>512 for k in obj):raise ValueError('预览字段无效')
            stack.extend(obj.values())
        elif isinstance(obj,list):stack.extend(obj)
        elif isinstance(obj,float) and not math.isfinite(obj):raise ValueError('预览数值无效')
        elif not isinstance(obj,(str,int,float,bool,type(None))):raise ValueError('预览字段无效')
        elif isinstance(obj,str) and len(obj)>10_000:raise ValueError('预览文本过长')


def validate_archive(archive_path, temporary):
    """Extract only two regular files and validate all referenced compressed assets."""
    archive_path=Path(archive_path);temporary=Path(temporary)
    if archive_path.stat().st_size>MAX_ARCHIVE:raise ValueError('预览超出大小限制')
    with zipfile.ZipFile(archive_path) as archive:
        files=archive.infolist()
        if len(files)!=3 or {x.filename for x in files}!={'index.json','source.html','metadata.json'}:raise ValueError('预览包文件无效')
        for item in files:
            mode=item.external_attr>>16
            if stat.S_ISLNK(mode) or item.is_dir() or item.compress_type!=zipfile.ZIP_STORED or item.flag_bits&1:raise ValueError('预览包文件无效')
            if item.file_size>(MAX_INDEX if item.filename=='index.json' else 8192 if item.filename=='metadata.json' else MAX_ARCHIVE):raise ValueError('预览包文件过大')
            with archive.open(item) as src,(temporary/item.filename).open('xb') as dst:shutil.copyfileobj(src,dst,1024**2)
    index=json.loads((temporary/'index.json').read_text(encoding='utf-8'));_finite_tree(index)
    metadata=json.loads((temporary/'metadata.json').read_text(encoding='utf-8'));_finite_tree(metadata)
    if not isinstance(metadata,dict) or not isinstance(metadata.get('note_name',''),str) or len(metadata.get('note_name',''))>80 or any(ord(c)<32 for c in metadata.get('note_name','')):raise ValueError('检查备注无效')
    if not isinstance(metadata.get('source_id',''),str) or not re.fullmatch(r'[a-f0-9]{32}',metadata.get('source_id','')):raise ValueError('检查编号无效')
    ct=index['ct'];scene=index['scene'];rois=index['rois'];assets=index['assets']
    if not isinstance(assets,dict) or len(assets)>100_000 or not isinstance(scene.get('models'),list) or len(scene['models'])>1000:raise ValueError('预览索引无效')
    if ct.get('codec')!='bricks-gzip' or ct.get('dtype') not in ('int16','int32','float32','float64'):raise ValueError('CT 类型无效')
    if not isinstance(ct.get('size'),list) or len(ct['size'])!=3 or any(type(n) is not int or n<1 or n>4096 for n in ct['size']) or math.prod(ct['size'])>2_000_000_000:raise ValueError('CT 尺寸无效')
    if not isinstance(ct.get('tiles'),list) or len(ct['tiles'])>100_000 or not ct['tiles']:raise ValueError('CT 分块无效')
    if ct.get('tile_size')!=[64,64,32]:raise ValueError('CT 分块格式无效')
    import numpy as np
    matrix=np.asarray(ct.get('matrix'),dtype=float);inverse=np.asarray(ct.get('inverse'),dtype=float);spacing=np.asarray(ct.get('spacing'),dtype=float);origin=np.asarray(ct.get('origin'),dtype=float);bounds=np.asarray(ct.get('bounds'),dtype=float)
    if matrix.shape!=(3,3) or inverse.shape!=(3,3) or spacing.shape!=(3,) or origin.shape!=(3,) or bounds.shape!=(2,3) or not all(np.isfinite(x).all() for x in (matrix,inverse,spacing,origin,bounds)) or np.any(spacing<=0) or not np.allclose(matrix@inverse,np.eye(3),atol=1e-5):raise ValueError('CT 空间坐标无效')
    dtype_bytes={'int16':2,'int32':4,'float32':4,'float64':8}[ct['dtype']];expected=[]
    for z in range(0,ct['size'][2],32):
        for y in range(0,ct['size'][1],64):
            for x in range(0,ct['size'][0],64):expected.append(([x,y,z],[min(64,ct['size'][0]-x),min(64,ct['size'][1]-y),min(32,ct['size'][2]-z)]))
    if len(expected)!=len(ct['tiles']):raise ValueError('CT 分块数量不匹配')
    for tile,(start,size) in zip(ct['tiles'],expected):
        if not isinstance(tile,dict) or tile.get('start')!=start or tile.get('size')!=size or not isinstance(tile.get('data'),dict) or tile['data'].get('bytes')!=math.prod(size)*dtype_bytes:raise ValueError('CT 分块范围不匹配')
    total=0;source_size=(temporary/'source.html').stat().st_size
    for key,record in assets.items():
        if not re.fullmatch(r'[a-f0-9]{64}',key) or not isinstance(record,dict):raise ValueError('预览索引无效')
        off,length,raw,compressed=[record.get(k) for k in ('offset','length','bytes','compressed_bytes')]
        if any(type(x) is not int for x in (off,length,raw,compressed)) or min(off,length,raw,compressed)<0 or raw>MAX_ASSET or compressed>MAX_ASSET or off+length>source_size or length!=4*((compressed+2)//3):raise ValueError('预览资源范围无效')
        total+=raw
        if total>MAX_INFLATED:raise ValueError('预览解压大小超出限制')
    from preview_access import Preview
    preview=Preview(temporary)
    for key,record in assets.items():
        compressed=preview.compressed(key)
        unpack=zlib.decompressobj(16+zlib.MAX_WBITS);raw=unpack.decompress(compressed,record['bytes']+1)
        if len(raw)!=record['bytes'] or not unpack.eof or unpack.unconsumed_tail or unpack.unused_data:raise ValueError('预览资源损坏')
    expected_digest=ct.get('raw_sha256')
    if not isinstance(expected_digest,str) or not re.fullmatch(r'[a-f0-9]{64}',expected_digest):raise ValueError('CT 校验值无效')
    if ct['size'][0]*ct['size'][1]*min(32,ct['size'][2])*dtype_bytes>MAX_ASSET:raise ValueError('CT 单层组过大')
    digest=hashlib.sha256();index_num=0
    for z in range(0,ct['size'][2],32):
        slab=np.empty((min(32,ct['size'][2]-z),ct['size'][1],ct['size'][0]),dtype=preview.dtype)
        for y in range(0,ct['size'][1],64):
            for x in range(0,ct['size'][0],64):
                tile=preview.tile(index_num);index_num+=1;slab[:,y:y+tile.shape[1],x:x+tile.shape[2]]=tile
        digest.update(memoryview(slab).cast('B'))
    if digest.hexdigest()!=expected_digest:raise ValueError('CT 像素校验失败')
    # Every referenced data block must exist and declare its exact byte count.
    stack=[scene,ct,rois]
    while stack:
        value=stack.pop()
        if isinstance(value,dict):
            if 'asset' in value:
                key=value.get('asset');size=value.get('bytes');record=assets.get(key)
                if not record or size!=record['bytes']:raise ValueError('预览资源引用无效')
            stack.extend(value.values())
        elif isinstance(value,list):stack.extend(value)
    return preview,metadata


def import_preview(server, package, actor, stage):
    """Validate and rebuild off to the side; publish only after a final state check."""
    from preview_package import write_preview
    stage=Path(stage);extracted=stage/'validated';extracted.mkdir()
    previous,metadata=validate_archive(package,extracted)
    digest=hashlib.sha256()
    with (extracted/'source.html').open('rb') as source:
        while block:=source.read(1024**2):digest.update(block)
    digest=digest.hexdigest();source_id=metadata['source_id']
    with server.LOCK:
        existing=None
        for path in server.JOBS.glob('*/status.json'):
            status=server.read(path)
            if status.get('transfer_source_id')==source_id and status.get('owner_id')==actor['id']:
                existing=(path.parent,status);break
            if path.parent.name==source_id and status.get('owner_id')==actor['id'] and not status.get('transfer_source_id'):
                original=path.parent/'output/preview.html'
                if status.get('state')!='completed' or not original.is_file():raise ValueError('本机原检查正在处理，无法导入')
                check=hashlib.sha256()
                with original.open('rb') as source:
                    while block:=source.read(1024**2):check.update(block)
                if check.hexdigest()==digest:return {'id':source_id,'unchanged':True}
                raise ValueError('该原检查已存在不同版本，未覆盖原始数据')
        if existing:
            job,status=existing
            if status['state']!='completed' or not status.get('data_pruned'):raise ValueError('服务器上的检查正在更新，请稍后重试')
            old_pointer=server.read(job/'output/online/current.json')
            if status.get('transfer_digest')==digest:return {'id':job.name,'unchanged':True}
            job_id=job.name
        else:
            job_id=uuid.uuid4().hex;job=server.JOBS/job_id;status=None;old_pointer=None
    candidate=stage/job_id;output=candidate/'output';output.mkdir(parents=True)
    receipt=write_preview(output,previous.index['scene'],previous.ct,previous.index['rois'],resolver=previous.compressed)
    server.write(output/'manifest.json',receipt)
    now=time.time()
    newstatus={**(status or {}),'id':job_id,'kind':'transferred','state':'completed','created_at':(status or {}).get('created_at',now),'updated_at':now,'finished_at':now,'owner_id':actor['id'],'owner_name':actor['username'],'data_pruned':True,'transfer_source_id':source_id,'transfer_digest':digest,'note_name':metadata.get('note_name','').strip(),'bytes':Path(package).stat().st_size,'files':1}
    with server.LOCK:
        if status is None:
            if job.exists():raise ValueError('服务器检查编号冲突')
            # Recheck source mapping after expensive work to prevent duplicates.
            for path in server.JOBS.glob('*/status.json'):
                state=server.read(path)
                if state.get('transfer_source_id')==source_id and state.get('owner_id')==actor['id']:raise ValueError('检查已在服务器更新，请重试')
            server.write(candidate/'status.json',newstatus);os.replace(candidate,job)
        else:
            if not job.exists() or server.read(job/'status.json')!=status or server.read(job/'output/online/current.json')!=old_pointer:raise ValueError('服务器检查已变化，请重试')
            old=job/'output';generation=old_pointer['generation']
            if not re.fullmatch(r'[a-f0-9]{32}',generation):raise ValueError('服务器预览版本无效')
            shutil.copytree(old/'online'/generation,output/'online'/generation,copy_function=os.link)
            backup=stage/'previous-output';os.replace(old,backup)
            try:
                os.replace(output,old);server.write(job/'status.json',newstatus)
            except BaseException:
                if old.exists():os.replace(old,output)
                os.replace(backup,old);raise
    return {'id':job_id,'unchanged':False}
