'use strict';
window.ReconstructionPhases={waiting:'待重建',segmenting:'分割中',meshing:'建模中',loading:'加载中',ready:'已生成',empty:'未生成',stopped:'已停止',interrupted:'未完成'};
// The only spatial placeholder is the source CT extent, never an invented organ.
window.createReconstructionPlaceholders=function({C,scene,meshMap,modelGroup,renderer,renderController}){
 const host=document.querySelector('#renderHost'),panel=document.createElement('div'),title=document.createElement('strong'),stage=document.createElement('span'),note=document.createElement('span');
 panel.className='reconstruction-placeholder is-initial';panel.setAttribute('role','status');panel.setAttribute('aria-live','polite');panel.dataset.placeholder='scan-range';
 title.textContent='模型占位';note.textContent='扫描范围，非解剖模型';note.className='placeholder-note';stage.className='placeholder-stage';panel.append(title,stage,note);host.append(panel);
 let frame=null,disposed=false,last='';
 if(!renderer.disabled){
  const [lo,hi]=C.bounds,size=hi.map((v,i)=>Math.max(.01,v-lo[i])),center=[-(lo[0]+hi[0])/2,-(lo[1]+hi[1])/2,(lo[2]+hi[2])/2],box=new THREE.BoxGeometry(...size),edges=new THREE.EdgesGeometry(box);box.dispose();
  const span=Math.max(...size),material=new THREE.LineDashedMaterial({color:ViewerUI.palette.darkSlate11,linewidth:.5,dashSize:span/35,gapSize:span/70,transparent:true,opacity:.45,depthWrite:false});
  frame=new THREE.LineSegments(edges,material);frame.position.set(...center);frame.computeLineDistances();frame.userData.reconstructionPlaceholder=true;scene.add(frame);
 }
 function disposeFrame(){if(frame&&!disposed){scene.remove(frame);frame.geometry.dispose();frame.material.dispose();disposed=true}}
 function update(record,loading=false){
  const plan=record.plan||[],real=[...meshMap.values()].filter(mesh=>renderer.disabled||!!mesh.geometry.index).length,pending=plan.filter(p=>!['ready','empty'].includes(p.phase));
  const active=pending.find(p=>['segmenting','meshing'].includes(p.phase)),next=active||pending[0];
  const initial=real===0&&modelGroup.visible,terminal=!!record.terminal,complete=!!record.complete,signature=JSON.stringify([initial,terminal,complete,loading,next?.id,next?.phase,pending.length,record.activity?.task,record.activity?.label]);
  if(signature===last)return;last=signature;
  panel.hidden=complete;
  panel.classList.toggle('is-initial',initial);note.hidden=!initial||terminal;
  title.textContent=terminal?(record.error?'重建未完成':'重建已停止'):initial?'模型占位':'正在重建';
  stage.textContent=loading?'加载已生成模型':!terminal&&!active&&record.activity?.label?record.activity.label:next?next.label+' '+(ReconstructionPhases[next.phase]||'待重建'):terminal?'已生成内容可继续查看':'整理预览';
  if(frame&&!disposed)frame.visible=initial&&!terminal&&!complete;
  if(real>0||terminal||complete)disposeFrame();renderController.invalidate();
 }
 return{update,dispose(){disposeFrame();panel.remove()}};
};
