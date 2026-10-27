"""Derived context ROIs for native CT visualization, never segmentation edits."""
from pathlib import Path
import base64,gzip,hashlib,json
import numpy as np
from scipy.ndimage import maximum_filter

ROOT=Path(__file__).resolve().parent
VERSION=3

def context_rois(scene,ct):
    geometry=ct['maskGeometry'];size=np.array(geometry['size']);spacing=np.array(geometry['spacing'])
    groups={};masks={m['id']:m for m in ct['masks']}
    for m in scene['models']:
        if m['id'] in masks and m['group']!='margin':groups.setdefault(m['group'],[]).append(m['id'])
    cache=ROOT/'cache/volume_rois';cache.mkdir(parents=True,exist_ok=True)
    included=[m for m in scene['models'] if m['id'] in masks and m['group']!='margin']
    result={'version':VERSION,'source':'segmentation_context_only','margin_mm':2.,'airway_margin_mm':0.,'ribcage_context_voxels':1,'rois':[]}
    for group,ids in groups.items():
        selected=[masks[sid] for sid in ids]
        key=hashlib.sha256(json.dumps([VERSION,geometry,group,selected],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        receipt=cache/(key+'.json')
        if receipt.exists():roi=json.loads(receipt.read_text('utf-8'))
        else:
            radius=np.zeros(3,dtype=int) if group=='airways' else np.ones(3,dtype=int) if group=='ribcage' else np.ceil(2./spacing).astype(int)
            lower=np.maximum(0,np.min([m['start'] for m in selected],axis=0)-radius)
            upper=np.minimum(size,np.max([np.array(m['start'])+m['size'] for m in selected],axis=0)+radius)
            crop=np.zeros(tuple((upper-lower)[::-1]),np.uint8)
            for m in selected:
                raw=np.frombuffer(gzip.decompress(base64.b64decode(m['data'])),np.uint8)
                bits=np.unpackbits(raw,bitorder='little')[:int(np.prod(m['size']))].reshape(m['size'][::-1])
                start=np.array(m['start'])-lower;end=start+m['size']
                crop[tuple(slice(int(a),int(b)) for a,b in zip(start[::-1],end[::-1]))]|=bits
            # Box context, not a smoothed or relabelled anatomical boundary.
            crop=maximum_filter(crop,size=tuple((radius*2+1)[::-1]),mode='constant')
            roi={'group':group,'size':(upper-lower).tolist(),'origin_lps':(np.array(geometry['origin'])+lower*spacing).tolist(),'spacing':spacing.tolist(),'encoding':'packed_bits_gzip','data':base64.b64encode(gzip.compress(np.packbits(crop.ravel(),bitorder='little').tobytes(),compresslevel=3,mtime=0)).decode(),'context_margin_axis_mm':(radius*spacing).tolist(),'segmentation_modified':False}
            temporary=receipt.with_suffix('.tmp');temporary.write_text(json.dumps(roi,separators=(',',':')),'utf-8');temporary.replace(receipt)
        result['rois'].append(roi)
    # Context classification for all chosen structures. This is not a new
    # anatomical segmentation: native HU, not this map, drives opacity.
    if 0<len(included)<255:
        key=hashlib.sha256(json.dumps(['mixed-v3-integrated-ribcage',geometry,[(m['id'],m['group'],masks[m['id']]) for m in included]],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        receipt=cache/('mixed-'+key+'.json')
        if receipt.exists():mixed=json.loads(receipt.read_text('utf-8'))
        else:
            selected=[masks[m['id']] for m in included];radius=np.ceil(2./spacing).astype(int)
            lower=np.maximum(0,np.min([m['start'] for m in selected],axis=0)-radius)
            upper=np.minimum(size,np.max([np.array(m['start'])+m['size'] for m in selected],axis=0)+radius)
            labels=np.zeros(tuple((upper-lower)[::-1]),np.uint8)
            priority={'lungs':0,'lobes':1,'heart':2,'aorta':3,'veins':4,'arteries':5,'airways':6,'ribs':7,'ribcage':7,'spine':7,'scapulae':7,'nodules':8,'reviewed_regions':9}
            for index,m in sorted(enumerate(included,1),key=lambda x:priority.get(x[1]['group'],0)):
                radius=np.zeros(3,dtype=int) if m['group']=='airways' else np.ones(3,dtype=int) if m['group']=='ribcage' else np.ceil(2./spacing).astype(int)
                item=masks[m['id']];lo=np.maximum(lower,np.array(item['start'])-radius);hi=np.minimum(upper,np.array(item['start'])+item['size']+radius)
                crop=np.zeros(tuple((hi-lo)[::-1]),np.uint8);raw=np.frombuffer(gzip.decompress(base64.b64decode(item['data'])),np.uint8)
                bits=np.unpackbits(raw,bitorder='little')[:int(np.prod(item['size']))].reshape(item['size'][::-1]);start=np.array(item['start'])-lo;stop=start+item['size']
                crop[tuple(slice(int(a),int(b)) for a,b in zip(start[::-1],stop[::-1]))]=bits
                crop=maximum_filter(crop,size=tuple((radius*2+1)[::-1]),mode='constant')
                a=lo-lower;b=hi-lower;view=labels[tuple(slice(int(x),int(y)) for x,y in zip(a[::-1],b[::-1]))];view[crop>0]=index
            mixed={'group':'layers','size':(upper-lower).tolist(),'origin_lps':(np.array(geometry['origin'])+lower*spacing).tolist(),'spacing':spacing.tolist(),'encoding':'uint8_gzip','data':base64.b64encode(gzip.compress(labels.tobytes(),compresslevel=3,mtime=0)).decode(),'model_ids':[m['id'] for m in included],'context_margin_axis_mm':(np.ceil(2./spacing)*spacing).tolist(),'airway_margin_mm':0.,'segmentation_modified':False}
            temporary=receipt.with_suffix('.tmp');temporary.write_text(json.dumps(mixed,separators=(',',':')),'utf-8');temporary.replace(receipt)
        result['rois'].insert(0,mixed)
    return result
