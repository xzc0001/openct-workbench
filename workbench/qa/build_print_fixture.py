from pathlib import Path
import shutil,base64,gzip
import numpy as np
from test_preview_loading import PreviewLoadingTests
from preview_package import write_preview,TILE
x=PreviewLoadingTests('test_bricks_recover_every_native_voxel_and_geometry_byte');x.setUp()
try:
 m=x.model.copy();v=np.array([[-30,-20,-30],[30,-20,-30],[30,20,-30],[-30,20,-30],[-30,-20,30],[30,-20,30],[30,20,30],[-30,20,30]],dtype='<f4');inds=np.array([0,1,2,0,2,3,4,6,5,4,7,6,0,4,5,0,5,1,1,5,6,1,6,2,2,6,7,2,7,3,3,7,4,3,4,0],dtype='<u4');norms=np.array([[-1,-1,-1],[1,-1,-1],[1,1,-1],[-1,1,-1],[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1]],dtype='<f4')/1.7320508;enc=lambda a:base64.b64encode(gzip.compress(a.tobytes(),mtime=0)).decode();m.update(vertices=len(v),triangles=len(inds)//3,positions=enc(v),indices=enc(inds),normals=enc(norms),bounds_ras=[-30,30,-20,20,-30,30]);scene={'models':[m]}
 out=Path(x.temp.name)/'other'/'output';out.mkdir(parents=True);write_preview(out,scene,x.ct,x.rois,(x.data[z:z+TILE[2]] for z in range(0,len(x.data),TILE[2])));shutil.copy2(out/'preview.html',Path(__file__).resolve().parent/'print-synthetic-preview.html')
finally:x.tearDown()
