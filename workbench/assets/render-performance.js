'use strict';
// Keep only the selected geometry on CPU/GPU. Rendering is demand driven.
window.createViewerRenderController=function({renderer,scene,camera,controls,meshes,state,geometryFrom,prepareGeometry,modelGroup,volumeRendering}){
 const $=s=>document.querySelector(s),host=$('#renderHost'),select=$('#render-quality'),info=$('#render-stats'),interactionQuality=$('#interaction-quality'),contextOption=$('#interaction-context'),momentumOption=$('#rotation-momentum');
 try{interactionQuality.checked=localStorage.getItem('thorax-interaction-quality-v1')!=='0'}catch{}
 try{momentumOption.checked=localStorage.getItem('thorax-rotation-momentum-v1')==='1'}catch{}
 contextOption.disabled=!interactionQuality.checked;
 let animation=null,lastAnimation=0,lastDampingFrame=0,frame=null,settle=null,interacting=false,pointerActive=false,hiddenContext=[],lost=false,loading=0;
 state.quality=state.initialQuality||'full';select.value=state.quality;controls.enableDamping=momentumOption.checked;controls.dampingFactor=.12;
 const records=new Map([...meshes.values()].map(mesh=>[mesh,{data:mesh.userData.lod?.[state.quality]||mesh.userData,quality:state.quality,loaded:!renderer.disabled}]));
 function notice(message){window.viewerNotice?.(message)}
 function restoreContext(){for(const mesh of hiddenContext)mesh.visible=!!mesh.userData.check?.checked;hiddenContext=[]}
 function adaptiveInteraction(){return interacting&&interactionQuality.checked}
 function updateContext(){restoreContext();if(adaptiveInteraction()&&contextOption.checked&&state.quality!=='full'){const context=new Set(['lungs','lobes','rll']);if([...meshes.values()].some(m=>m.visible&&!context.has(m.userData.group)))for(const mesh of meshes.values())if(mesh.visible&&mesh.material.transparent&&context.has(mesh.userData.group)){hiddenContext.push(mesh);mesh.visible=false}}}
 function begin(){clearTimeout(settle);if(interacting)return;interacting=true;updateContext();resize();invalidate()}
 function finish(){clearTimeout(settle);if(!interacting)return;interacting=false;restoreContext();resize();invalidate()}
 // OrbitControls has no reset-velocity API. Flush its pending deltas while
 // preserving the visible camera/target, so disabling momentum never jumps.
 function clearMomentum(){if(!controls.enableDamping)return;const position=camera.position.clone(),target=controls.target.clone(),up=camera.up.clone();controls.enableDamping=false;controls.update();camera.position.copy(position);controls.target.copy(target);camera.up.copy(up);controls.update();controls.enableDamping=momentumOption.checked&&!animation}
 function stopMomentum(){clearMomentum();if(!animation&&!pointerActive)finish();invalidate()}
 function end(){pointerActive=false;lastDampingFrame=performance.now();clearTimeout(settle);if(controls.enableDamping&&!animation)invalidate();else settle=setTimeout(finish,120)}
 function resize(){const width=Math.max(1,host.clientWidth),height=Math.max(1,host.clientHeight),q=state.quality,adaptive=adaptiveInteraction(),cap=(volumeRendering.active?{light:adaptive?50000:250000,balanced:adaptive?90000:600000,full:adaptive?140000:1400000}:{light:adaptive?280000:700000,balanced:adaptive?550000:1200000,full:adaptive?1000000:2200000})[q],ratio=Math.min(devicePixelRatio,({light:.9,balanced:1,full:1.5})[q],Math.sqrt(cap/(width*height)));renderer.setPixelRatio(ratio);renderer.setSize(width,height);const oldFit=Math.max(1,1/camera.aspect),newFit=Math.max(1,height/width);camera.position.sub(controls.target).multiplyScalar(newFit/oldFit).add(controls.target);camera.aspect=width/height;camera.updateProjectionMatrix();invalidate()}
 async function refreshGeometry(requested=state.quality){
  if(!modelGroup.visible||renderer.disabled)return true;
  const version=++loading,quality=requested,needed=[...meshes.values()].reduce((n,mesh)=>{const d=mesh.userData.lod?.[quality]||mesh.userData;return n+d.vertices*24+d.triangles*12},0);
  if(needed>(state.geometryBudget||256*1024**2)){notice('完整表面超出当前设备的显示预算，请使用标准或轻量模型');if(quality!=='light')return refreshGeometry('light');return false}
  const staged=[];
  try{
   for(const mesh of meshes.values()){
    const record=records.get(mesh),data=mesh.userData.lod?.[quality]||mesh.userData;
    staged.push({mesh,record,data,geometry:null});
    if(!record.loaded||record.data!==data)await prepareGeometry(data);
    if(version!==loading)return false;
   }
   // Prepare all buffers before changing the visible quality of any structure.
   for(const item of staged)if(!item.record.loaded||item.record.data!==item.data)item.geometry=geometryFrom(item.data);
   for(const {mesh,record,data,geometry} of staged){
    const simple=quality!=='full';if(Boolean(mesh.material.isMeshLambertMaterial)!==simple){const old=mesh.material;mesh.material=new (simple?THREE.MeshLambertMaterial:THREE.MeshStandardMaterial)({color:old.color.clone(),opacity:old.opacity,transparent:old.transparent,depthWrite:old.depthWrite,side:THREE.DoubleSide,clippingPlanes:old.clippingPlanes});mesh.material.forceSinglePass=simple;old.dispose()}
    if(geometry){mesh.geometry.dispose();if(record.data!==data)delete record.data.arrays;mesh.geometry=geometry}
    record.data=data;record.loaded=true;record.quality=quality;
   }
   state.quality=quality;select.value=quality;resize();invalidate();return true;
  }catch(error){for(const item of staged)if(item.record.data!==item.data){delete item.data.arrays;if(item.geometry&&item.mesh.geometry!==item.geometry)item.geometry.dispose()}throw error}
  finally{if(version!==loading)for(const item of staged)if(item.record.data!==item.data)delete item.data.arrays}
 }
 function releaseGeometry(){loading++;for(const mesh of meshes.values()){mesh.geometry.dispose();mesh.geometry=new THREE.BufferGeometry();const record=records.get(mesh);delete record.data.arrays;record.loaded=false}}
 function invalidate(){if(frame===null&&!document.hidden&&!lost)frame=requestAnimationFrame(render)}
 function render(){frame=null;if(document.hidden||lost)return;try{let dampingChanged=false;if(!animation&&controls.enableDamping){const now=performance.now(),elapsed=lastDampingFrame?Math.max(1/240,Math.min(.25,(now-lastDampingFrame)/1000)):1/60;lastDampingFrame=now;controls.dampingFactor=pointerActive ? .12 : 1-Math.exp(-8*elapsed);dampingChanged=controls.update();if(!dampingChanged&&!pointerActive&&interacting){clearMomentum();finish()}}if(animation){const now=performance.now(),delta=lastAnimation?Math.min(.1,(now-lastAnimation)/1000):0;lastAnimation=now;animation(delta)}volumeRendering.beforeRender(adaptiveInteraction(),state.quality);const gl=renderer.getContext();if(!renderer.disabled)for(let i=0;i<8&&gl.getError()!==gl.NO_ERROR;i++);renderer.render(scene,camera);if(!renderer.disabled&&!gl.isContextLost()){const error=gl.getError();if(error!==gl.NO_ERROR)throw Error('三维显存或绘制失败（'+error+'）')}const triangles=renderer.info.render.triangles;info.textContent=({light:'浏览简化',balanced:'浏览标准',full:'完整表面'})[state.quality]+' / '+(triangles/1000).toFixed(1)+' 千三角形 / '+(state.quality==='full'?'未经平滑/简化':'浏览代理')+' / '+(interacting?'旋转中':'静止不重绘');if(animation||dampingChanged)invalidate()}catch(error){console.warn(error);animation=null;document.dispatchEvent(new Event('viewer-three-paused'));notice('三维显示资源不足，正在恢复结构表面');if(volumeRendering.active)volumeRendering.fallback?.(error.message,'light');else if(state.quality!=='light'){state.quality='light';select.value='light';releaseGeometry();refreshGeometry().then(invalidate).catch(error=>{notice('三维恢复失败，请重新加载预览');$('#reload-three').hidden=false;console.warn(error)})}else notice('当前设备无法显示三维模型，断层影像可继续查看')}}
 if(renderer.debug)renderer.debug.onShaderError=()=>{throw Error('三维着色器无法在当前设备运行')};
 select.onchange=async()=>{select.disabled=true;const previous=state.quality;try{stopMomentum();const requested=select.value;if(volumeRendering.active){state.quality=requested;resize();invalidate()}else if(!await refreshGeometry(requested))select.value=state.quality;else if(state.quality===requested)notice('');try{localStorage.setItem(volumeRendering.active?'thorax-volume-quality-v2':'thorax-surface-quality-v2',state.quality)}catch{}}catch(error){state.quality=previous;select.value=previous;notice(error.status===404?'预览已更新，请重新加载':'模型读取失败，已保留原显示质量');if(error.status===404)$('#reload-three').hidden=false;console.error(error)}finally{select.disabled=false}};
 controls.addEventListener('change',invalidate);controls.addEventListener('start',()=>{clearMomentum();pointerActive=true;begin()});controls.addEventListener('end',end);
 contextOption.onchange=()=>{updateContext();invalidate()};
 interactionQuality.onchange=()=>{try{localStorage.setItem('thorax-interaction-quality-v1',interactionQuality.checked?'1':'0')}catch{}contextOption.disabled=!interactionQuality.checked;updateContext();resize();invalidate()};
 momentumOption.onchange=()=>{clearMomentum();controls.enableDamping=momentumOption.checked&&!animation;try{localStorage.setItem('thorax-rotation-momentum-v1',momentumOption.checked?'1':'0')}catch{}if(!pointerActive&&!animation)finish();invalidate()};
 document.addEventListener('visibilitychange',()=>{if(document.hidden){if(frame!==null)cancelAnimationFrame(frame);frame=null;pointerActive=false;clearMomentum();finish()}else{lastAnimation=0;if(animation)begin();invalidate()}});
 renderer.domElement.addEventListener('webglcontextlost',event=>{event.preventDefault();lost=true;pointerActive=false;clearMomentum();animation=null;document.dispatchEvent(new Event('viewer-three-paused'));state.playing=false;$('#reload-three').hidden=false;try{localStorage.setItem('thorax-surface-quality-v2','light')}catch{};$('#auto-play').textContent='播放旋转';$('#auto-play').setAttribute('aria-pressed','false');if(frame!==null)cancelAnimationFrame(frame);frame=null;notice('三维显示中断，正在等待浏览器恢复；断层影像可继续查看')});
 renderer.domElement.addEventListener('webglcontextrestored',async()=>{try{lost=true;state.quality='light';select.value='light';renderer.resetState?.();await volumeRendering.fallback?.('显存中断后已恢复轻量结构表面','light');await refreshGeometry();for(const mesh of meshes.values()){for(const attribute of Object.values(mesh.geometry.attributes))attribute.needsUpdate=true;if(mesh.geometry.index)mesh.geometry.index.needsUpdate=true;mesh.material.needsUpdate=true}const gl=renderer.getContext();for(let i=0;i<8&&gl.getError()!==gl.NO_ERROR;i++);lost=false;$('#reload-three').hidden=true;invalidate()}catch(error){notice('三维恢复失败，请重新加载预览');$('#reload-three').hidden=false;console.warn(error)}});
 $('#reload-three').onclick=()=>location.reload();resize();return{invalidate,resize,refreshGeometry,releaseGeometry,stopMomentum,register(mesh){records.set(mesh,{data:mesh.userData.lod?.[state.quality]||mesh.userData,quality:state.quality,loaded:false})},setAnimation(callback,keepInteraction=false){clearMomentum();animation=callback;controls.enableDamping=momentumOption.checked&&!callback;lastAnimation=0;if(callback)begin();else if(!keepInteraction)finish();invalidate()},renderNow(){stopMomentum();finish();if(frame!==null)cancelAnimationFrame(frame);frame=null;render()},get interacting(){return interacting},async setQuality(value){select.value=value;await select.onchange()},geometryStats(){return[...meshes.values()].map(m=>({id:m.userData.id,visible:m.visible,triangles:m.geometry.index?.count/3||0,sourceTriangles:m.userData.triangles}))}};
};
