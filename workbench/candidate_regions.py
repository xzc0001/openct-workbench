"""Experimental density-region proposals, not nodule diagnosis or a validated GGN detector."""
from pathlib import Path
import json,hashlib,math,time
import numpy as np
import SimpleITK as sitk
from scipy import ndimage as ndi
PARAMETERS={'version':2,'analysis_spacing_mm':1.5,'density_min_hu':-850,'density_max_hu':-250,'scales_mm':[2.,4.,6.],'response_floor_hu':10.,'max_candidates':40,'minimum_volume_mm3':12.,'maximum_volume_mm3':18000.}

def region_identity(image,source_sha):
 metadata=[image.GetSize(),*[np.round(values,5).tolist() for values in (image.GetSpacing(),image.GetOrigin(),image.GetDirection())]]
 return hashlib.sha256(source_sha.encode()+json.dumps(metadata,separators=(',',':')).encode()+sitk.GetArrayFromImage(image).astype(np.uint8).tobytes()).hexdigest()

def atomic_json(path,value):
 temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)

def propose(array,lung,excluded,spacing=1.5,parameters=None,manual_seeds=None):
 p={**PARAMETERS,**(parameters or {})};a=array.astype(np.float32,copy=False);smooth=ndi.gaussian_filter(a,1/spacing);background=ndi.gaussian_filter(a,7/spacing);valid=lung&~excluded&(smooth>=p['density_min_hu'])&(smooth<=p['density_max_hu']);peaks=[]
 for scale in p['scales_mm']:
  sigma=scale/spacing;response=-ndi.gaussian_laplace(a,sigma)*sigma*sigma
  values=np.where(valid,response,-np.inf);maxima=(values==ndi.maximum_filter(values,size=5))&(values>=p['response_floor_hu']);coords=np.argwhere(maxima)
  if len(coords):
   scores=values[tuple(coords.T)];order=np.argsort(scores)[-180:][::-1];peaks.extend((float(scores[i]),float(scale),coords[i]) for i in order)
 peaks.sort(key=lambda item:item[0],reverse=True);peaks=[(1e9,4.,np.asarray(seed,dtype=int)) for seed in (manual_seeds or [])]+peaks;results=[]
 for response,scale,seed in peaks:
  if len(results)>=p['max_candidates']:break
  if any(np.linalg.norm((seed-r['seed_zyx'])*spacing)<max(5,min(scale*2,r['scale_mm']*2)) for r in results):continue
  radius=min(18,max(8,scale*2.8));padding=int(math.ceil(radius/spacing));lo=np.maximum(0,seed-padding);hi=np.minimum(a.shape,seed+padding+1);block=tuple(slice(int(l),int(h)) for l,h in zip(lo,hi));local_seed=seed-lo;z,y,x=np.ogrid[0:hi[0]-lo[0],0:hi[1]-lo[1],0:hi[2]-lo[2]];sphere=((z-local_seed[0])**2+(y-local_seed[1])**2+(x-local_seed[2])**2)*spacing**2<=radius**2
  threshold=max(p['density_min_hu'],float(background[tuple(seed)])+20);region=valid[block]&sphere&(smooth[block]>=threshold);components,_=ndi.label(region);label=components[tuple(local_seed)]
  if label==0:continue
  mask=components==label;count=int(mask.sum());volume=count*spacing**3
  if not p['minimum_volume_mm3']<=volume<=p['maximum_volume_mm3']:continue
  coords=np.argwhere(mask);shape=(coords.max(0)-coords.min(0)+1)*spacing
  if np.max(shape)>38:continue
  eigen=np.linalg.eigvalsh(np.cov(coords.T)) if len(coords)>3 else np.ones(3);elongation=float(np.sqrt(max(eigen[-1],.01)/max(eigen[0],.1)))
  if elongation>8:continue
  center=coords.mean(0)+lo
  # Avoid duplicate proposals whose masks overlap a previously retained center.
  if any(np.all((r['center_zyx']>=lo)&(r['center_zyx']<hi)) and mask[tuple(np.rint(r['center_zyx']-lo).astype(int).clip(0,np.array(mask.shape)-1))] for r in results):continue
  duplicate=False
  for previous in results:
   old_lo=previous['start_zyx'];old_hi=old_lo+np.array(previous['mask'].shape);over_lo=np.maximum(lo,old_lo);over_hi=np.minimum(hi,old_hi)
   if np.any(over_hi<=over_lo):continue
   new_block=tuple(slice(int(l),int(h)) for l,h in zip(over_lo-lo,over_hi-lo));old_block=tuple(slice(int(l),int(h)) for l,h in zip(over_lo-old_lo,over_hi-old_lo));overlap=np.count_nonzero(mask[new_block]&previous['mask'][old_block])
   if overlap>.2*min(count,previous['mask'].sum()):duplicate=True;break
  if duplicate:continue
  values=a[block][mask];flags=[]
  if np.any(ndi.binary_dilation(mask)&~lung[block]):flags.append('near_lung_boundary')
  if elongation>3:flags.append('elongated_region')
  results.append({'seed_zyx':seed,'center_zyx':center,'start_zyx':lo,'mask':mask,'scale_mm':scale,'volume_mm3':float(volume),'mean_hu':float(values.mean()),'density_range_hu':[float(np.percentile(values,10)),float(np.percentile(values,90))],'local_response_hu':(None if response==1e9 else response),'manual_seed':response==1e9,'elongation':elongation,'flags':flags})
 return results

def screen(output,parameters=None,manual_points_lps=None):
 from PIL import Image,ImageDraw
 output=Path(output);p={**PARAMETERS,**(parameters or {})};source=output/'CT.nii.gz';volume=sitk.ReadImage(str(source));spacing=p['analysis_spacing_mm'];size=[max(1,int(math.ceil((n-1)*s/spacing))+1) for n,s in zip(volume.GetSize(),volume.GetSpacing())];ref=sitk.Image(size,sitk.sitkFloat32);ref.SetSpacing([spacing]*3);ref.SetOrigin(volume.GetOrigin());ref.SetDirection(volume.GetDirection());image=sitk.Resample(volume,ref,sitk.Transform(),sitk.sitkLinear,-1024,sitk.sitkFloat32);a=sitk.GetArrayFromImage(image)
 def load(path):
  if not path.exists():return np.zeros(a.shape,bool)
  return sitk.GetArrayFromImage(sitk.Resample(sitk.ReadImage(str(path)),ref,sitk.Transform(),sitk.sitkNearestNeighbor,0,sitk.sitkUInt8))>0
 lung=load(output/'masks/lungs.nii.gz')
 if not lung.any():
  for task in ['lung_highres','total']:
   for path in (output/'ai'/task).glob('lung_*lobe*.nii.gz'):lung|=load(path)
 if not lung.any():raise ValueError('需先完成肺轮廓或肺叶分割，才能在肺内筛选可疑区域')
 excluded=np.zeros(a.shape,bool)
 for path in [output/'ai/lung_vessels/lung_arteries.nii.gz',output/'ai/lung_vessels/lung_veins.nii.gz',output/'ai/lung_vessels/lung_airways.nii.gz',output/'masks/lung_nodule_candidates.nii.gz',output/'masks/airways.nii.gz',output/'masks/pulmonary_arteries.nii.gz',output/'masks/pulmonary_veins.nii.gz']:
  excluded|=load(path)
 excluded=ndi.binary_dilation(excluded,iterations=1)
 manual_seeds=[]
 for point in manual_points_lps or []:
  index=np.rint(image.TransformPhysicalPointToContinuousIndex(tuple(map(float,point))))[::-1].astype(int)
  if np.any(index<0) or np.any(index>=a.shape) or not lung[tuple(index)]:raise ValueError('手工定位点不在当前肺标签范围内，请核对坐标与检查')
  if not p['density_min_hu']<=float(ndi.gaussian_filter(a,1/spacing)[tuple(index)])<=p['density_max_hu']:raise ValueError('手工定位点不在当前低密度筛选范围内，请核对位置；不会强行生成病灶')
  manual_seeds.append(index)
 start=time.monotonic();regions=propose(a,lung,excluded,spacing,p,manual_seeds);
 if manual_seeds and not any(r['manual_seed'] for r in regions):raise ValueError('手工点未形成可分离的低密度区域，请核对位置；不会强行生成病灶')
 folder=output/'candidate_regions';folder.mkdir(exist_ok=True);rows=[]
 with source.open('rb') as stream:source_sha=hashlib.file_digest(stream,'sha256').hexdigest()
 identity_path=output/'candidate_identity.json';identity=json.loads(identity_path.read_text(encoding='utf-8')) if identity_path.exists() else {}
 previous_path=output/'candidate_review.json';previous=json.loads(previous_path.read_text(encoding='utf-8')) if previous_path.exists() else {}
 if previous and previous.get('source_ct_sha256')!=source_sha:raise ValueError('候选来源 CT 已变更')
 for entry in previous.get('candidates',[]):
  path=folder/entry['mask_file']
  if path.exists():identity.setdefault(region_identity(sitk.ReadImage(str(path)),source_sha),entry['id'])
 counter=max([int(sid.rsplit('_',1)[1]) for sid in identity.values()]+[int(path.name.split('.')[0].rsplit('_',1)[1]) for path in folder.glob('suspect_*.nii.gz')]+[0])
 status_path=output.parent/'status.json';selected=json.loads(status_path.read_text(encoding='utf-8')).get('candidate_ids',[]) if status_path.exists() else []
 rows=[entry for entry in previous.get('candidates',[]) if entry['id'] in selected]
 for index,r in enumerate(regions,1):
  if len(rows)>=p['max_candidates']:break
  mask=r['mask'];lo=r['start_zyx'];cropped=sitk.GetImageFromArray(mask.astype(np.uint8));cropped.SetSpacing(image.GetSpacing());cropped.SetDirection(image.GetDirection());cropped.SetOrigin(image.TransformIndexToPhysicalPoint(tuple(map(int,lo[::-1]))))
  fingerprint=region_identity(cropped,source_sha)
  if fingerprint not in identity:counter+=1;identity[fingerprint]=f'suspect_{counter:03d}'
  sid=identity[fingerprint]
  if any(entry['id']==sid for entry in rows):continue
  if not (folder/(sid+'.nii.gz')).exists():sitk.WriteImage(cropped,str(folder/(sid+'.nii.gz')),True)
  center_lps=image.TransformContinuousIndexToPhysicalPoint(tuple(map(float,r['center_zyx'][::-1])));center_ras=[-center_lps[0],-center_lps[1],center_lps[2]];native_index=volume.TransformPhysicalPointToContinuousIndex(center_lps);seed=np.rint(r['center_zyx']).astype(int);canvas=Image.new('RGB',(480,160),(15,25,35))
  for view in range(3):
   axis=[0,1,2][view];slices=[]
   for k,n in enumerate(a.shape):slices.append(int(np.clip(seed[k],0,n-1)) if k==axis else slice(max(0,int(seed[k]-32)),min(n,int(seed[k]+33))))
   patch=a[tuple(slices)];gray=np.clip((patch+1350)/1500*255,0,255).astype(np.uint8)
   if view:gray=gray[::-1]
   thumb=Image.fromarray(gray).convert('RGB');thumb=thumb.resize((160,160));draw=ImageDraw.Draw(thumb);remaining=[k for k in range(3) if k!=axis];v=(seed[remaining[0]]-slices[remaining[0]].start)/max(1,patch.shape[0]-1);u=(seed[remaining[1]]-slices[remaining[1]].start)/max(1,patch.shape[1]-1);cx=int(u*159);cy=int((1-v if view else v)*159);draw.line((cx-4,cy,cx+4,cy),fill=(66,230,210));draw.line((cx,cy-4,cx,cy+4),fill=(66,230,210));draw.text((5,5),['AX','COR','SAG'][view],fill='white');canvas.paste(thumb,(view*160,0))
  if not (folder/(sid+'.png')).exists():canvas.save(folder/(sid+'.png'))
  row={'id':sid,'name':f'候选 {int(sid.rsplit("_",1)[1]):02d}','center_ras_mm':center_ras,'center_lps_mm':list(center_lps),'center_ijk':list(native_index),'volume_mm3':r['volume_mm3'],'mean_hu':r['mean_hu'],'density_range_hu':r['density_range_hu'],'local_response_hu':r['local_response_hu'],'flags':r['flags'],'mask_file':sid+'.nii.gz','thumbnail_file':sid+'.png','manual_seed':r['manual_seed'],'review_status':'experimental_region_not_diagnosis'};rows.append(row)
 record={'version':2,'source_ct_sha256':source_sha,'parameters':p,'candidates':rows,'seconds':round(time.monotonic()-start,2),'method':'Multi-scale local density proposals inside lung labels, excluding available vessel/airway/nodule masks. Scores are HU responses, not probabilities. No validated GGN recall or diagnostic meaning.','limitations':['May miss ground-glass regions and propose normal vessels, partial-volume changes or inflammation.','Analysis uses a 1.5 mm grid; displayed geometry is an approximate density region.','Review source CT before choosing regions.']}
 atomic_json(identity_path,identity);atomic_json(output/'candidate_review.json',record);return record
if __name__=='__main__':
 import sys
 record=screen(Path(sys.argv[1]));print(json.dumps({'count':len(record['candidates']),'seconds':record['seconds']},ensure_ascii=False),flush=True)
