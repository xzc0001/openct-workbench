"""Bounded native-HU access for LAN planes and explicitly requested 3D crops."""
from collections import OrderedDict
from pathlib import Path
import base64
import gzip
import hashlib
import json
import threading
import numpy as np

LIMIT = 32*1024**2
CACHE = OrderedDict()
CACHE_BYTES = 0
LOCK = threading.RLock()
SLOTS = threading.BoundedSemaphore(2)
DTYPES = {'int16':'<i2','int32':'<i4','float32':'<f4','float64':'<f8'}


class Preview:
    def __init__(self, directory):
        self.directory=Path(directory)
        if self.directory.is_symlink():raise ValueError('Invalid preview directory')
        self.index=json.loads((self.directory/'index.json').read_text(encoding='utf-8'))
        self.ct=self.index['ct']
        self.dtype=np.dtype(DTYPES[self.ct['dtype']])

    def compressed(self, key):
        record=self.index['assets'].get(key)
        if not record or record['compressed_bytes']>128*1024**2:raise ValueError('Unknown preview asset')
        path=self.directory/'source.html'
        if path.is_symlink():raise ValueError('Invalid preview source')
        with path.open('rb') as source:
            source.seek(record['offset']);data=base64.b64decode(source.read(record['length']),validate=True)
        if hashlib.sha256(data).hexdigest()!=key:raise ValueError('Preview asset checksum failed')
        return data

    def tile(self, index):
        global CACHE_BYTES
        tile=self.ct['tiles'][index];key=(str(self.directory),tile['data']['asset'])
        with LOCK:
            if key in CACHE:
                value=CACHE.pop(key);CACHE[key]=value;return value.reshape(tile['size'][::-1])
        raw=gzip.decompress(self.compressed(tile['data']['asset']))
        if len(raw)!=int(np.prod(tile['size']))*self.dtype.itemsize:raise ValueError('Invalid native CT brick')
        value=np.frombuffer(raw,dtype=self.dtype).reshape(tile['size'][::-1])
        with LOCK:
            if key in CACHE:return CACHE[key].reshape(tile['size'][::-1])
            while CACHE and CACHE_BYTES+value.nbytes>LIMIT:
                _,old=CACHE.popitem(last=False);CACHE_BYTES-=old.nbytes
            CACHE[key]=value;CACHE_BYTES+=value.nbytes
        return value

    def sample(self, coordinates):
        q=[np.floor(v+.5).astype(np.int64) for v in coordinates]
        shape=q[0].shape;valid=np.ones(shape,bool)
        for axis in range(3):valid&=(q[axis]>=0)&(q[axis]<self.ct['size'][axis])
        result=np.full(shape,-1024,dtype=self.dtype)
        if not valid.any():return result
        x,y,z=[v[valid] for v in q];tx,ty,tz=self.ct['tile_size']
        nx=(self.ct['size'][0]+tx-1)//tx;ny=(self.ct['size'][1]+ty-1)//ty
        ids=x//tx+nx*(y//ty+ny*(z//tz));values=np.empty(x.shape,dtype=self.dtype)
        for index in np.unique(ids):
            use=ids==index;tile=self.tile(int(index));origin=self.ct['tiles'][int(index)]['start']
            values[use]=tile[z[use]-origin[2],y[use]-origin[1],x[use]-origin[0]]
        result[valid]=values
        return result

    def plane(self, request):
        width,height=int(request['width']),int(request['height'])
        if not 2<=width<=2048 or not 2<=height<=4096 or width*height>4*1024**2:raise ValueError('Invalid plane size')
        points=np.array([request[k] for k in ('p00','p10','p01')],dtype=np.float64)
        if points.shape!=(3,3) or not np.isfinite(points).all() or np.abs(points).max()>100000:raise ValueError('Invalid plane coordinates')
        inverse=np.array(self.ct['inverse']);origin=np.array(self.ct['origin'])
        start=inverse@(points[0]-origin)
        dx=inverse@((points[1]-points[0])/(width-1));dy=inverse@((points[2]-points[0])/(height-1))
        result=np.empty((height,width),dtype=self.dtype);x=np.arange(width)[None,:]
        for row in range(0,height,32):
            y=np.arange(row,min(row+32,height))[:,None]
            result[row:row+len(y)]=self.sample([start[k]+dy[k]*y+dx[k]*x for k in range(3)])
        return result

    def volume(self, scope, budget):
        budget=min(128*1024**2,int(budget))
        start,size=region(self.ct,self.index['rois'],scope)
        if int(np.prod(size))*self.dtype.itemsize>budget:raise ValueError('该范围超出显存预算，请缩小显示范围或使用结构表面')
        result=np.empty(tuple(size[::-1]),dtype=self.dtype)
        tx,ty,tz=self.ct['tile_size'];nx=(self.ct['size'][0]+tx-1)//tx;ny=(self.ct['size'][1]+ty-1)//ty
        end=start+size
        for z in range(start[2]//tz,(end[2]+tz-1)//tz):
            for y in range(start[1]//ty,(end[1]+ty-1)//ty):
                for x in range(start[0]//tx,(end[0]+tx-1)//tx):
                    index=x+nx*(y+ny*z);brick=self.ct['tiles'][index];a=np.maximum(start,brick['start']);b=np.minimum(end,np.array(brick['start'])+brick['size'])
                    src=tuple(slice(int(v),int(w)) for v,w in zip((a-brick['start'])[::-1],(b-brick['start'])[::-1]))
                    dst=tuple(slice(int(v),int(w)) for v,w in zip((a-start)[::-1],(b-start)[::-1]))
                    result[dst]=self.tile(index)[src]
        return result,start,size


def region(ct,rois,scope):
    size=np.array(ct['size'],dtype=int)
    if not scope:return np.zeros(3,dtype=int),size
    roi=next((r for r in rois['rois'] if r['group']==scope),None)
    if not roi:raise ValueError('Unknown CT display range')
    lo=np.array(roi['origin_lps']);hi=lo+(np.array(roi['size'])-1)*roi['spacing']
    import itertools
    corners=np.array(list(itertools.product(*zip(lo,hi))))
    q=(corners-np.array(ct['origin']))@np.array(ct['inverse']).T
    halo=np.maximum(2,np.ceil(1./np.array(ct['spacing'])))
    start=np.clip(np.floor(q.min(axis=0))-halo,0,size-1).astype(int)
    end=np.clip(np.ceil(q.max(axis=0))+halo+1,start+1,size).astype(int)
    return start,end-start
