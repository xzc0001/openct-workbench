'use strict';
// Poll a small manifest; native CT bricks and immutable geometry are fetched once.
window.startProgressiveViewer=function({D,C,scene,masks,meshMap,modelGroup,renderer,renderController,draw,remount}){
 if(!D.stream||!C.transport)return;
 const $=s=>document.querySelector(s);let revision=-1,stopped=false,timer=null;
 const placeholders=createReconstructionPlaceholders({C,scene,meshMap,modelGroup,renderer,renderController});
 placeholders.update({plan:D.stream.plan||[],complete:false,terminal:false});
 if(!D.models.length)$('#layer-empty').textContent='模型生成中';
 const complete=document.createElement('a');complete.textContent='完整预览';complete.hidden=true;complete.className='progressive-complete';$('#about').before(complete);
 async function apply(record){
  const signature=JSON.stringify([record.revision,record.state,record.terminal,record.error]);
  record.plan=(record.plan||D.stream.plan||[]).map(item=>({...item,phase:record.terminal&&!['ready','empty'].includes(item.phase)?(record.error?'interrupted':'stopped'):item.phase}));
  if(signature===revision){placeholders.update(record);return}
  for(const model of record.models){
   const existing=meshMap.get(model.id);
   if(existing){Object.assign(existing.userData,Object.fromEntries(Object.entries(model).filter(([key])=>!['positions','indices','normals','lod','arrays','encoding','color','opacity','check'].includes(key))));continue}
   model.color=ViewerUI.modelColor(model);
   if(['airways','arteries','veins','aorta'].includes(model.group))model.opacity=1;
   const material=new THREE.MeshStandardMaterial({color:new THREE.Color(...model.color),side:THREE.DoubleSide,transparent:model.opacity<1,opacity:model.opacity,roughness:1,depthWrite:model.opacity>=1});
   const mesh=new THREE.Mesh(new THREE.BufferGeometry(),material);mesh.visible=model.group!=='margin'&&model.visible!==false;mesh.userData=model;
   meshMap.set(model.id,mesh);modelGroup.add(mesh);renderController.register(mesh);
  }
  const loading=!renderer.disabled&&modelGroup.visible&&[...meshMap.values()].some(m=>!m.geometry.index);
  if(loading){
   D.reconstruction_plan=record.plan.map(item=>({...item,phase:[...meshMap.values()].some(m=>m.userData.group===item.group&&!m.geometry.index)?'loading':item.phase}));
   remount();placeholders.update({...record,plan:D.reconstruction_plan},true);
   const loaded=await renderController.refreshGeometry();if(!loaded)throw Error('模型正在等待可用的显示资源');
  }
  for(const model of record.models)if(!D.models.some(m=>m.id===model.id))D.models.push(meshMap.get(model.id).userData);
  for(const mask of record.masks)if(!masks.some(m=>m.id===mask.id))masks.push({...mask,bits:null});
  D.nodules=record.nodules||[];
  const target=$('#target'),selected=target.value;while(target.options.length>1)target.remove(1);
  for(const n of D.nodules){const option=document.createElement('option');option.value=n.id;option.textContent=ViewerUI.cleanName(n.name);target.append(option)}target.value=selected;
  D.reconstruction_plan=record.plan;remount();placeholders.update(record);
  $('#summary').textContent=D.models.length?D.models.length+' 个结构':'CT 已就绪';
  draw();renderController.invalidate();revision=signature;
 }
 async function tick(){
  if(stopped)return;
  try{
   if(document.hidden)return;
   const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000);
   let response;try{response=await fetch(D.stream.snapshot,{cache:'no-store',signal:controller.signal})}finally{clearTimeout(timeout)}
   if(!response.ok){if(response.status===404){stopped=true;$('#status').textContent='预览已更新，请打开当前检查';$('#reload-three').hidden=false}throw Error('重建进度暂不可读取')}
   const record=await response.json();await apply(record);
   if(!D.models.length)$('#layer-empty').textContent=record.terminal?'未生成模型':'模型生成中';
   $('#status').textContent=record.complete?'重建完成，自动分割待复核':record.terminal?(record.error?'重建未完成，影像仍可查看':'重建已停止，影像仍可查看'):'重建中 '+Math.round(record.progress?.percent||0)+'%';
   if(record.preview){complete.href=record.preview;complete.textContent=record.complete?'完整预览':'上一版';complete.hidden=false}
   if(record.terminal)stopped=true;
  }catch(error){if(!stopped)$('#status').textContent='正在重连重建进度，影像可继续查看'}
  finally{if(!stopped)timer=setTimeout(tick,3000)}
 }
 window.addEventListener('pagehide',()=>{stopped=true;clearTimeout(timer);placeholders.dispose()},{once:true});tick();
};
