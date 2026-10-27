"""Upgrade completed local previews while preserving the previous HTML."""
import json,re,shutil,sys,time
from pathlib import Path
import SimpleITK as sitk
import pydicom
from viewer_pack import pack,original_volume,encode_mask
ROOT=Path(__file__).resolve().parent
for name in sys.argv[1:]:
 job=ROOT/'jobs'/name;out=job/'output';target=out/'preview.html'
 if (out/'scene.json').exists(): scene=json.loads((out/'scene.json').read_text(encoding='utf-8'))
 else: scene=json.loads(re.search(r'<script id="sceneData"[^>]*>(.*?)</script>',target.read_text(encoding='utf-8'),re.S)[1])
 if 'ribs' in scene.get('selected_structures',[]):
  from structures import RIB_NAMES
  scene['missing_structures']=[name for name in RIB_NAMES if name not in {m['id'] for m in scene['models']}]
 old=out/'revisions'/('preview-'+time.strftime('%Y%m%d-%H%M%S')+'.html');old.parent.mkdir(exist_ok=True);shutil.copy2(target,old)
 canonical=sitk.ReadImage(str(out/'CT.nii.gz'));native,original=original_volume(job,canonical);packed=[]
 for m in scene['models']:
  if m['group'] in {'airways','arteries','veins'}:m['opacity']=1
  path=out/'masks'/m.get('mask_file',m['id']+'.nii.gz')
  if path.exists():packed.append(encode_mask(m,sitk.GetArrayFromImage(sitk.ReadImage(str(path)))!=0))
 status=json.loads((job/'status.json').read_text(encoding='utf-8'));series=next(s for s in json.loads((job/'inventory.json').read_text(encoding='utf-8'))['series'] if s['id']==status['series_id'])
 instances=[int(getattr(pydicom.dcmread(p,stop_before_pixels=True,specific_tags=['InstanceNumber']),'InstanceNumber',i+1)) for i,p in enumerate(series['files'])]
 receipt=pack(out,canonical,scene,packed,native,original,instances)
 (out/'scene.json').write_text(json.dumps(scene,ensure_ascii=False),encoding='utf-8');manifest=json.loads((out/'manifest.json').read_text(encoding='utf-8'));manifest.update(receipt);(out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');print(name,json.dumps(receipt),flush=True)
