"""Refresh completed indexed previews while retaining prior preview data."""
from pathlib import Path
import hashlib,json,os,shutil,sys,time
root=Path(sys.argv[1]);os.chdir(root);sys.path.insert(0,str(root))
from preview_access import Preview
from preview_package import refresh_indexed
active={'uploading','queued_scan','scanning','queued','running'}
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def summary(preview):return {'ct':digest(preview.ct),'scene':digest(preview.index['scene']),'rois':digest(preview.index['rois']),'assets':digest(sorted(preview.index['assets']))}
for status_path in sorted((root/'jobs').glob('*/status.json')):
 status=json.loads(status_path.read_text(encoding='utf-8'));output=status_path.parent/'output';pointer=output/'online/current.json';preview_file=output/'preview.html';manifest=output/'manifest.json'
 if status.get('state')!='completed' or not all(p.is_file() for p in (pointer,preview_file,manifest)):continue
 if any(json.loads(q.read_text(encoding='utf-8')).get('state') in active for q in (root/'jobs').glob('*/status.json')):raise RuntimeError('active work detected; remaining previews unchanged')
 old=json.loads(pointer.read_text(encoding='utf-8'))['generation'];previous=Preview(output/'online'/old);scene=previous.index['scene'];review=output/'candidate_review.json'
 if review.exists():
  rows=json.loads(review.read_text(encoding='utf-8'))['candidates'];updated={**scene,'review_candidates':[row for row in rows if row['id'] not in {m['id'] for m in scene['models']}]}
  if digest(updated)!=digest(scene):print(status_path.parent.name,'skipped: candidate metadata differs');continue
 before=summary(previous);old_pointer=pointer.read_bytes();old_manifest=manifest.read_bytes();backup=output/'revisions'/('before-a4-print-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir(parents=True,exist_ok=False)
 try:os.link(preview_file,backup/'preview.html')
 except OSError:shutil.copy2(preview_file,backup/'preview.html')
 (backup/'current.json').write_bytes(old_pointer);(backup/'manifest.json').write_bytes(old_manifest)
 receipt=None
 try:
  receipt=refresh_indexed(preview_file,require_completed=True,keep_previous=True)
  generation=json.loads(pointer.read_text(encoding='utf-8'))['generation'];now=Preview(output/'online'/generation)
  if summary(now)!=before:raise ValueError('CT, structures or metadata differ after refresh')
  entry=(output/'online'/generation/'index.html').read_text(encoding='utf-8')
  if 'id="print-open"' not in entry or 'createViewerPrintComposer' not in entry:raise ValueError('print controls missing')
  if not (output/'online'/old/'index.json').exists():raise ValueError('old generation missing')
  print(status_path.parent.name,'updated; data and previous generation verified',flush=True)
 except BaseException:
  current=json.loads(pointer.read_text(encoding='utf-8')) if pointer.is_file() else {};state=json.loads(status_path.read_text(encoding='utf-8')).get('state')
  if state=='completed' and (current.get('generation')==old or (receipt and current.get('generation')==receipt.get('online_generation'))):
   original=backup/'preview.html';temporary=output/'preview.html.restore'
   try:os.link(original,temporary)
   except OSError:shutil.copy2(original,temporary)
   os.replace(temporary,preview_file);temp=pointer.with_name('current.restore');temp.write_bytes(old_pointer);os.replace(temp,pointer);temp=manifest.with_name('manifest.restore');temp.write_bytes(old_manifest);os.replace(temp,manifest)
  else:print(status_path.parent.name,'changed concurrently; automatic rollback skipped',flush=True)
  raise
