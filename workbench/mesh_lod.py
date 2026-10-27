"""Cache display-only topology-preserving LOD meshes. Never alters source masks/STL."""
from pathlib import Path
import base64,hashlib,json,gzip,uuid
ROOT=Path(__file__).resolve().parent
BUDGETS={
 'light':{'aorta':10000,'airways':12000,'arteries':12000,'veins':12000,'heart':6000,'lungs':3500,'lobes':2200,'ribs':1500,'ribcage':50000,'spine':14000,'scapulae':5000,'rll':3500,'margin':6000,'reviewed_regions':6000},
 'balanced':{'aorta':30000,'airways':40000,'arteries':40000,'veins':40000,'heart':18000,'lungs':12000,'lobes':7000,'ribs':4500,'ribcage':150000,'spine':40000,'scapulae':15000,'rll':12000,'margin':18000,'reviewed_regions':16000}}

def display_lods(scene, progress_callback=None):
 import numpy as np
 import vtk
 from vtk.util.numpy_support import numpy_to_vtk,numpy_to_vtkIdTypeArray,vtk_to_numpy
 enc=lambda a:base64.b64encode(gzip.compress(a.tobytes(),compresslevel=6,mtime=0)).decode('ascii')
 def samples(poly,limit=2048):
  v=vtk_to_numpy(poly.GetPoints().GetData());ix=vtk_to_numpy(poly.GetPolys().GetConnectivityArray()).reshape(-1,3)
  return np.concatenate((v[np.linspace(0,len(v)-1,min(limit,len(v)),dtype=int)],v[ix[np.linspace(0,len(ix)-1,min(limit,len(ix)),dtype=int)]].mean(axis=1)))
 def distances(source,target):
  function=vtk.vtkImplicitPolyDataDistance();function.SetInput(target);values=vtk.vtkDoubleArray()
  function.FunctionValue(numpy_to_vtk(samples(source),deep=True),values)
  return np.abs(vtk_to_numpy(values))
 cache=ROOT/'cache/mesh_lod';cache.mkdir(parents=True,exist_ok=True)
 result=dict(scene);result['models']=[]
 for original in scene['models']:
  m={k:v for k,v in original.items() if k!='lod'};m['lod']={}
  raw=lambda key:gzip.decompress(base64.b64decode(m[key])) if m.get('encoding')=='gzip' else base64.b64decode(m[key])
  points=np.frombuffer(raw('positions'),dtype='<f4').reshape(-1,3)
  indices=np.frombuffer(raw('indices'),dtype='<u4').reshape(-1,3)
  source_normals=np.frombuffer(raw('normals'),dtype='<f4').reshape(-1,3)
  signature=hashlib.sha256(points.tobytes()+indices.tobytes()).hexdigest()
  poly=None
  for quality,budgets in BUDGETS.items():
   # Small targets remain exact; no generated proxy for candidate nodules.
   budget=budgets.get(m['group'],len(indices))
   if len(indices)<=budget:continue
   if progress_callback:progress_callback('生成'+m.get('name',m['id'])+'的'+{'light':'轻量','balanced':'标准'}[quality]+'显示模型')
   tolerance={'light':1.,'balanced':.5}[quality]
   receipt=cache/(signature+f'-reference-v6-{quality}-tol{tolerance}-{budget}.json')
   if receipt.exists():lod=json.loads(receipt.read_text(encoding='utf-8'))
   else:
    if poly is None:
     pts=vtk.vtkPoints();pts.SetData(numpy_to_vtk(points,deep=True));cells=vtk.vtkCellArray();cells.SetData(numpy_to_vtkIdTypeArray(np.arange(0,(len(indices)+1)*3,3,dtype=np.int64),deep=True),numpy_to_vtkIdTypeArray(indices.ravel().astype(np.int64),deep=True));poly=vtk.vtkPolyData();poly.SetPoints(pts);poly.SetPolys(cells)
    # A face target is only a performance hint. Reject proxies that fail
    # the engineering sampling gate; never call these clinical tolerances.
    accepted=False
    for attempt in range(5):
     dec=vtk.vtkDecimatePro();dec.SetInputData(poly);dec.SetTargetReduction(1-budget/len(indices));dec.SetFeatureAngle(180);dec.PreserveTopologyOn();dec.BoundaryVertexDeletionOff();dec.SplittingOff();dec.SetErrorIsAbsolute(1);dec.SetAbsoluteError(tolerance*tolerance*.1*(.1**attempt));dec.AccumulateErrorOn();dec.Update()
     proxy=dec.GetOutput()
     errors=np.concatenate((distances(poly,proxy),distances(proxy,poly)))
     if errors.max()<=tolerance:
      accepted=True;break
    if not accepted or proxy.GetNumberOfPolys()>=len(indices):
     m.setdefault('proxy_fallbacks',{})[quality]='full_reference_used_sampling_gate_not_met'
     continue
    proxy.GetPointData().SetNormals(None);proxy.GetCellData().SetNormals(None)
    normals=vtk.vtkPolyDataNormals();normals.SetInputData(proxy);normals.ConsistencyOn();normals.AutoOrientNormalsOn();normals.SplittingOff();normals.Update();out=normals.GetOutput()
    v=vtk_to_numpy(out.GetPoints().GetData()).astype('<f4');ix=vtk_to_numpy(out.GetPolys().GetConnectivityArray()).astype('<u4');n=vtk_to_numpy(out.GetPointData().GetNormals()).astype('<f4')
    if not np.isfinite(v).all() or len(ix)%3 or ix.max()>=len(v):raise ValueError('Invalid display proxy')
    deviation={'method':'bidirectional_vertex_and_triangle_centroid_sampling','samples':len(errors),'max_sampled_mm':float(errors.max()),'p95_sampled_mm':float(np.quantile(errors,.95)),'certified_bound':False,'clinical_validation':False,'engineering_sampling_gate_mm':tolerance}
    lod={'encoding':'gzip','positions':enc(v),'indices':enc(ix),'normals':enc(n),'vertices':len(v),'triangles':len(ix)//3,'requested_triangle_budget':budget,'topology_preserved':True,'display_only':True,'bounds_ras':list(out.GetBounds()),'deviation_from_reference':deviation}
    temp=receipt.with_name(receipt.name+'.'+uuid.uuid4().hex+'.tmp');temp.write_text(json.dumps(lod,separators=(',',':')),encoding='utf-8');temp.replace(receipt)
   m['lod'][quality]=lod
  # Compress the unchanged full-resolution reference too; compression is lossless.
  m.update(encoding='gzip',positions=enc(points.astype('<f4')),indices=enc(indices.astype('<u4')),normals=enc(source_normals.astype('<f4')))
  result['models'].append(m)
 result['display_lod_version']=6
 return result
