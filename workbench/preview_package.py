"""One lossless offline file, indexed assets and a small LAN entry page.

Online assets are ranges of the same hard-linked HTML, not duplicate CT files.
CT bricks keep native dtype and affine. No segmentation or geometry changes.
"""
from pathlib import Path
import base64
import copy
import gzip
import hashlib
import io
import json
import mmap
import os
import re
import shutil
import tempfile
import uuid
import zlib
import numpy as np

ROOT = Path(__file__).resolve().parent
TILE = (64, 64, 32)  # xyz; at most 256 KiB for int16
VERSION = 4


def inline(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')


def template():
    page = (ROOT/'assets/viewer.v2.template.html').read_text(encoding='utf-8')
    for token, name in [('__STYLE__','viewer.css'),('__THREE__','three.min.js'),('__ORBIT__','OrbitControls.js'),('__LOADER__','viewer-source.js'),('__UI__','viewer-ui.js'),('__VOLUME_RENDERER__','native-volume.js'),('__PERFORMANCE__','render-performance.js'),('__INTERACTIONS__','viewer-interactions.js'),('__PLACEHOLDERS__','reconstruction-placeholders.js'),('__PROGRESSIVE__','progressive-viewer.js'),('__APP__','viewer.js')]:
        page = page.replace(token, (ROOT/'assets'/name).read_text(encoding='utf-8-sig'))
    for filename, title in [('THREE-LICENSE.txt', 'Three.js / OrbitControls'), ('RADIX-COLORS-LICENSE.txt', 'Radix UI Colors 3.0.0')]:
        license_text = (ROOT/'assets'/filename).read_text(encoding='utf-8').replace('--', '- -')
        page = page.replace('</body>', '<!-- '+title+' MIT license\n'+license_text+'\n-->\n</body>')
    return page


class Assets:
    def __init__(self, stream, resolver=None):
        self.stream, self.index, self.resolver = stream, {}, resolver

    def add(self, compressed, raw_bytes):
        if len(compressed)<18 or compressed[:2]!=b'\x1f\x8b' or int.from_bytes(compressed[-4:], 'little') != raw_bytes % 2**32:
            raise ValueError('Invalid lossless preview asset')
        key = hashlib.sha256(compressed).hexdigest()
        if key not in self.index:
            self.stream.write(f'<script id="asset-{key}" type="application/octet-stream">'.encode())
            start = self.stream.tell()
            encoded = base64.b64encode(compressed)
            self.stream.write(encoded)
            self.stream.write(b'</script>\n')
            self.index[key] = {'offset':start, 'length':len(encoded), 'bytes':raw_bytes, 'compressed_bytes':len(compressed)}
        return {'asset':key, 'bytes':raw_bytes}

    def array(self, value):
        array = np.ascontiguousarray(value)
        return self.add(gzip.compress(memoryview(array).cast('B'), compresslevel=3, mtime=0), array.nbytes)

    def encoded(self, value, raw_bytes, codec='gzip'):
        if isinstance(value, dict):return self.add(self.resolver(value['asset']),value['bytes'])
        data = base64.b64decode(value, validate=True)
        return self.add(data if codec=='gzip' else gzip.compress(data,compresslevel=3,mtime=0), raw_bytes)


def mesh_assets(model, assets):
    result = {key:value for key,value in model.items() if key not in {'positions','indices','normals','lod','arrays'}}
    for key in ('positions','normals','indices'):
        if key in model:
            count = model['triangles'] if key=='indices' else model['vertices']
            result[key] = assets.encoded(model[key],count*12,model.get('encoding'))
    result['encoding']='gzip'
    if 'lod' in model:result['lod']={key:mesh_assets(lod,assets) for key,lod in model['lod'].items()}
    return result


def write_preview(output, scene, ct, rois, slabs=None, notify=lambda message:None, resolver=None, before_publish=None, online_base=None):
    output = Path(output)
    generation = uuid.uuid4().hex
    online = output/'online'/generation
    online.mkdir(parents=True)
    pending = output/'preview.html.tmp'
    dtype = np.dtype({'int16':'<i2','int32':'<i4','float32':'<f4','float64':'<f8'}[ct['dtype']])
    try:
        with tempfile.TemporaryFile(dir=output) as spool:
            assets = Assets(spool,resolver)
            view_scene = {**scene, 'models':[mesh_assets(m,assets) for m in scene['models']], 'viewer_version':VERSION}
            view_ct = {key:value for key,value in ct.items() if key not in {'data','masks','transport','tiles','codec'}}
            view_ct.update(codec='bricks-gzip', tile_size=list(TILE), tiles=[], masks=[])
            for mask in ct.get('masks',[]):
                count=int(np.prod(mask['size']))
                view_ct['masks'].append({**mask,'data':assets.encoded(mask['data'],(count+7)//8)})
            view_rois=copy.deepcopy(rois)
            for roi in view_rois.get('rois',[]):
                count=int(np.prod(roi['size']));length=count if roi['encoding']=='uint8_gzip' else (count+7)//8
                roi['data']=assets.encoded(roi['data'],length)
            digest=hashlib.sha256();z=0
            notify('整理原始 CT 分块')
            for slab in slabs or []:
                slab=np.ascontiguousarray(slab,dtype=dtype)
                if slab.shape[1:] != tuple(ct['size'][1::-1]) or not 0<slab.shape[0]<=TILE[2]:
                    raise ValueError('Unexpected native CT slab dimensions')
                digest.update(memoryview(slab).cast('B'))
                for y in range(0,ct['size'][1],TILE[1]):
                    for x in range(0,ct['size'][0],TILE[0]):
                        brick=slab[:,y:y+TILE[1],x:x+TILE[0]]
                        view_ct['tiles'].append({'start':[x,y,z],'size':list(brick.shape[::-1]),'data':assets.array(brick)})
                z+=slab.shape[0]
            if ct.get('codec')=='bricks-gzip':
                view_ct['tiles']=[{**brick,'data':assets.encoded(brick['data'],brick['data']['bytes'])} for brick in ct['tiles']]
                view_ct['raw_sha256']=ct['raw_sha256']
            else:
                if z!=ct['size'][2]:raise ValueError('Native CT volume truncated')
                view_ct['raw_sha256']=digest.hexdigest()
            # Surface viewing allocates no full-CT GPU texture at startup.
            view_scene['preferred_render_mode']='surface'
            page=template().replace('__SCENE_DATA__',inline(view_scene)).replace('__VOLUME_ROIS__',inline(view_rois))
            prefix,suffix=page.replace('__CT_DATA__',inline(view_ct)).split('__BINARY_ASSETS__')
            encoded_prefix=prefix.encode('utf-8');whole_digest=hashlib.sha256()
            with pending.open('wb') as target:
                def emit(data):target.write(data);whole_digest.update(data)
                emit(encoded_prefix);spool.seek(0)
                while block:=spool.read(1024**2):emit(block)
                emit(suffix.encode('utf-8'))
            for record in assets.index.values():record['offset']+=len(encoded_prefix)
            # Readers pin a generation, so updates never mix old metadata/new bytes.
            try:os.link(pending,online/'source.html')
            except OSError:shutil.copyfile(pending,online/'source.html')
            index={'version':VERSION,'generation':generation,'scene':view_scene,'ct':view_ct,'rois':view_rois,'assets':assets.index,'preview_sha256':whole_digest.hexdigest()}
            (online/'index.json').write_text(inline(index),encoding='utf-8')
            online_ct={**view_ct,'transport':{'base':(online_base or f'/results/{output.parent.name}/view')+'/'+generation}}
            entry=page.replace('__BINARY_ASSETS__','').replace('__CT_DATA__',inline(online_ct)).replace("connect-src 'none'", "connect-src 'self'")
            (online/'index.html').write_text(entry,encoding='utf-8')
            notify('写入三维预览')
            if before_publish:before_publish()
            pending.replace(output/'preview.html')
            pointer=output/'online/current.tmp'
            pointer.write_text(json.dumps({'generation':generation}),encoding='utf-8')
            pointer.replace(output/'online/current.json')
            for old in (output/'online').iterdir():
                if old.is_dir() and old.name!=generation and re.fullmatch('[a-f0-9]{32}',old.name):
                    if old.is_symlink():raise ValueError('Invalid generated preview directory')
                    shutil.rmtree(old)
        counts={q:sum(m.get('lod',{}).get(q,m)['triangles'] for m in scene['models']) for q in ('light','balanced','full')}
        return {'viewer_version':VERSION,'online_generation':generation,'preview_sha256':whole_digest.hexdigest(),'preview_bytes':(output/'preview.html').stat().st_size,'display_triangle_counts':counts,'ct_raw_sha256':view_ct['raw_sha256'],'native_axial_original':ct.get('original',False),'mpr_source_size':ct['size']}
    except BaseException:
        shutil.rmtree(online)
        pending.unlink(missing_ok=True)
        raise


def legacy_parts(path):
    """Read metadata without constructing the hundred-MiB CT base64 string."""
    with Path(path).open('rb') as source, mmap.mmap(source.fileno(),0,access=mmap.ACCESS_READ) as page:
        def bounds(name):
            match=re.search(rb'<script id="'+name+rb'"[^>]*>',page)
            if not match:raise ValueError('Missing preview metadata')
            return match.end(),page.find(b'</script>',match.end())
        a,b=bounds(b'sceneData');scene=json.loads(page[a:b])
        a,b=bounds(b'volumeRoiData');rois=json.loads(page[a:b])
        a,b=bounds(b'ctData');data=re.compile(rb'"data"\s*:\s*"').search(page,a,b)
        if not data:raise ValueError('Preview already uses indexed data')
        start=data.end();end=page.find(b'"',start)
        ct=json.loads(page[a:start]+page[end:b]);ct.pop('data')
        # Slabs are decompressed incrementally. At most one slab and 1 MiB input.
        def slabs():
            decompressor=zlib.decompressobj(16+zlib.MAX_WBITS);buffer=bytearray();dtype=np.dtype({'int16':'<i2','int32':'<i4','float32':'<f4','float64':'<f8'}[ct['dtype']])
            length=ct['size'][0]*ct['size'][1]*TILE[2]*dtype.itemsize
            for offset in range(start,end,1024**2):
                block=base64.b64decode(page[offset:min(end,offset+1024**2)],validate=True)
                while block:
                    buffer.extend(decompressor.decompress(block,length-len(buffer)))
                    block=decompressor.unconsumed_tail
                    if len(buffer)==length:
                        yield np.frombuffer(buffer,dtype=dtype).reshape(TILE[2],ct['size'][1],ct['size'][0])
                        buffer=bytearray()
            buffer.extend(decompressor.flush())
            if not decompressor.eof:raise ValueError('Truncated compressed CT')
            if buffer:yield np.frombuffer(buffer,dtype=dtype).reshape(-1,ct['size'][1],ct['size'][0])
            page.close();source.close()
        yield scene,ct,rois,slabs()


def upgrade(path, notify=print, scene_override=None, rebuild_lods=False):
    path=Path(path)
    with_index=path.parent/'manifest.json'
    manifest=json.loads(with_index.read_text(encoding='utf-8'))
    if manifest.get('viewer_version',0)>=VERSION:return manifest
    initial=path.stat()
    def guard():
        current=path.stat()
        if (current.st_ino,current.st_size,current.st_mtime_ns)!=(initial.st_ino,initial.st_size,initial.st_mtime_ns):raise ValueError('Preview changed during repackaging')
        status=path.parent.parent/'status.json'
        if status.is_file() and json.loads(status.read_text(encoding='utf-8'))['state']!='completed':raise ValueError('Study became active during repackaging')
    for scene,ct,rois,slabs in legacy_parts(path):
        if scene_override is not None:scene=scene_override
        if rebuild_lods:
            from mesh_lod import display_lods
            scene=display_lods(scene)
        review=path.parent/'candidate_review.json'
        if review.is_file():scene['review_candidates']=[row for row in json.loads(review.read_text(encoding='utf-8'))['candidates'] if row['id'] not in {m['id'] for m in scene['models']}]
        receipt=write_preview(path.parent,scene,ct,rois,slabs,notify,before_publish=guard)
    manifest.update(receipt)
    temporary=with_index.with_suffix('.tmp');temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(with_index)
    return receipt


def refresh_indexed(path, scene_override=None, rebuild_lods=False):
    from preview_access import Preview
    path=Path(path);output=path.parent
    pointer=json.loads((output/'online/current.json').read_text())
    previous=Preview(output/'online'/pointer['generation'])
    scene=scene_override or previous.index['scene']
    if rebuild_lods:
        from mesh_lod import display_lods
        if scene_override is None:
            scene=json.loads((output/'scene.json').read_text(encoding='utf-8'))
        scene=display_lods(scene)
    review=output/'candidate_review.json'
    if review.is_file():
        rows=json.loads(review.read_text(encoding='utf-8'))['candidates']
        scene={**scene,'review_candidates':[row for row in rows if row['id'] not in {m['id'] for m in scene['models']}]}
    receipt=write_preview(output,scene,previous.ct,previous.index['rois'],resolver=previous.compressed)
    manifest=output/'manifest.json';data=json.loads(manifest.read_text(encoding='utf-8'))
    data.update(receipt);temporary=manifest.with_suffix('.tmp');temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(manifest)
    return receipt
