'use strict';
// Key light stays at screen upper left as the observer orbits the anatomy.
window.ViewerLighting=Object.freeze({direction:Object.freeze([-.8,1,1.5]),ambient:.55,key:.95,volumeAmbient:.45,volumeDiffuse:.55});
window.createViewerLighting=function({THREE,scene,camera}){
 const ambient=new THREE.AmbientLight(0xffffff,ViewerLighting.ambient);scene.add(ambient);scene.add(camera);
 const light=new THREE.DirectionalLight(0xffffff,ViewerLighting.key);light.position.set(...ViewerLighting.direction);light.target.position.set(0,0,0);light.castShadow=false;camera.add(light,light.target);
 return{ambient,lights:[light]};
};
// Fast affine source-CT sampling. Optional contours use the original segmentation grid.
window.renderSlicePixels=function({ctx,w,h,p00,p10,p01,C,hu,pixels,ww,wl,active,overlay,boundaries}){
 const image=ctx.createImageData(w,h),out=image.data,stepX=p10.map((n,i)=>(n-p00[i])/(w-1)),stepY=p01.map((n,i)=>(n-p00[i])/(h-1));
 const inverse=C.inverse,origin=C.origin,g=C.maskGeometry,sx=C.size[0],sy=C.size[1],sz=C.size[2];const index=p=>inverse.map(row=>row.reduce((v,n,i)=>v+n*(p[i]-origin[i]),0)),q0=index(p00),qx=inverse.map(row=>row.reduce((v,n,i)=>v+n*stepX[i],0)),qy=inverse.map(row=>row.reduce((v,n,i)=>v+n*stepY[i],0));const models=active,edges=boundaries?Array.from({length:Math.ceil(models.length/32)},()=>new Uint32Array(w*h)):[];
 for(let y=0;y<h;y++){let px=p00[0]+stepY[0]*y,py=p00[1]+stepY[1]*y,pz=p00[2]+stepY[2]*y,ix=q0[0]+qy[0]*y,iy=q0[1]+qy[1]*y,iz=q0[2]+qy[2]*y;for(let x=0;x<w;x++,px+=stepX[0],py+=stepX[1],pz+=stepX[2],ix+=qx[0],iy+=qx[1],iz+=qx[2]){const a=Math.round(ix),b=Math.round(iy),c=Math.round(iz),value=a<0||b<0||c<0||a>=sx||b>=sy||c>=sz?-1024:pixels?pixels[y*w+x]:hu[a+sx*(b+sy*c)],gray=Math.max(0,Math.min(255,Math.round((value-wl+ww/2)*255/ww))),pixel=y*w+x,offset=pixel*4;out[offset]=out[offset+1]=out[offset+2]=gray;out[offset+3]=255;let filled=false;
 for(let j=0;j<models.length;j++){const m=models[j],mx=Math.round((px-g.origin[0])/g.spacing[0])-m.start[0],my=Math.round((py-g.origin[1])/g.spacing[1])-m.start[1],mz=Math.round((pz-g.origin[2])/g.spacing[2])-m.start[2];if(mx<0||my<0||mz<0||mx>=m.size[0]||my>=m.size[1]||mz>=m.size[2])continue;const bit=mx+m.size[0]*(my+m.size[1]*mz);if(!(m.bits[bit>>3]&(1<<(bit&7))))continue;if(boundaries)edges[j>>>5][pixel]|=1<<(j&31);if(overlay&&!filled){for(let k=0;k<3;k++)out[offset+k]=Math.round(gray*.55+m.color[k]*255*.45);filled=true;if(!boundaries)break}}
 }}
 if(boundaries)for(let y=0;y<h-1;y++)for(let x=0;x<w-1;x++){const i=y*w+x;let bit=-1;for(let word=0;word<edges.length;word++){const e=edges[word],changed=((e[i]^e[i+1])|(e[i]^e[i+w]))>>>0;if(changed){bit=word*32+31-Math.clz32((changed&-changed)>>>0);break}}if(bit<0)continue;const color=models[bit]?.color||ViewerUI.rgb(ViewerUI.palette.amber9);for(let k=0;k<3;k++)out[i*4+k]=Math.round(color[k]*255)}
 return image;
};
window.createViewerInteractions=function({D,C,state,views,scene,camera,controls,renderController,draw,center}){
 const $=s=>document.querySelector(s);let windowDrag=null,playing=false;const axis=new THREE.Vector3(0,0,1);
 for(const [name,V] of Object.entries(views)){const host=V.canvas.parentElement;host.oncontextmenu=e=>e.preventDefault();host.addEventListener('pointerdown',e=>{if(e.button!==2)return;e.preventDefault();windowDrag={id:e.pointerId,x:e.clientX,y:e.clientY,ww:Number($('#ww').value),wl:Number($('#wl').value)};host.setPointerCapture(e.pointerId)});host.addEventListener('pointermove',e=>{if(!windowDrag||windowDrag.id!==e.pointerId)return;const d=windowDrag,scale=Math.max(1,d.ww/300);$('#ww').value=Math.round(Math.max(1,Math.min(10000,d.ww+(e.clientX-d.x)*scale)));$('#wl').value=Math.round(Math.max(-3000,Math.min(10000,d.wl-(e.clientY-d.y)*scale)));draw()});const release=e=>{if(windowDrag?.id===e.pointerId)windowDrag=null};host.addEventListener('pointerup',release);host.addEventListener('pointercancel',release)}
 const marker=new THREE.Group(),material=new THREE.MeshBasicMaterial({color:0x12a594,depthTest:false,depthWrite:false});const dot=new THREE.Mesh(new THREE.SphereGeometry(2,10,8),material);marker.add(dot);const points=[];for(let k=0;k<3;k++){const a=[0,0,0],b=[0,0,0];a[k]=-7;b[k]=7;points.push(...a,...b)}const lines=new THREE.LineSegments(new THREE.BufferGeometry().setAttribute('position',new THREE.Float32BufferAttribute(points,3)),new THREE.LineBasicMaterial({color:0x12a594,depthTest:false,depthWrite:false}));marker.add(lines);marker.renderOrder=999;dot.renderOrder=999;lines.renderOrder=999;marker.visible=false;scene.add(marker);
 const focus=()=>new THREE.Vector3(-state.point[0],-state.point[1],state.point[2]);function rotationCenter(){const mode=$('#rotation-center').value;if(mode==='focus')return focus();if(mode==='model')return center.clone();return controls.target.clone()}
 function sync(){marker.position.copy(focus());marker.visible=$('#focus-marker').checked;if($('#rotation-center').value==='focus')controls.target.copy(focus());renderController.invalidate()}
 $('#copy-focus').onclick=async()=>{const text=JSON.stringify(state.point.map(v=>Math.round(v*1000)/1000));try{await navigator.clipboard.writeText(text);$('#copy-focus').textContent='已复制'}catch{const t=document.createElement('textarea');t.value=text;document.body.append(t);t.select();const ok=document.execCommand('copy');t.remove();$('#copy-focus').textContent=ok?'已复制':'复制失败'}};$('#focus-marker').oninput=sync;$('#boundaries').oninput=draw;$('#rotation-center').onchange=()=>{renderController.stopMomentum();controls.target.copy(rotationCenter());controls.update();renderController.invalidate()};$('#rotation-speed').oninput=()=>{$('#rotation-speed-value').textContent=$('#rotation-speed').value+'°/s'};
 function stop(keepInteraction=false){playing=false;state.playing=false;$('#auto-play').textContent='播放旋转';$('#auto-play').setAttribute('aria-pressed','false');renderController.setAnimation(null,keepInteraction)}
 function tick(seconds){const pivot=rotationCenter();controls.target.copy(pivot);const angle=Number($('#rotation-speed').value)*Math.PI/180*seconds;const offset=camera.position.clone().sub(pivot);if(Math.hypot(offset.x,offset.y)<.01)camera.up.applyAxisAngle(axis,angle);camera.position.sub(pivot).applyAxisAngle(axis,angle).add(pivot);controls.update()}
 document.addEventListener('viewer-three-paused',()=>stop());$('#auto-play').onclick=()=>{if(playing){stop();return}playing=true;state.playing=true;$('#auto-play').textContent='暂停旋转';$('#auto-play').setAttribute('aria-pressed','true');renderController.setAnimation(tick)};controls.addEventListener('start',()=>{if(playing)stop(true)});document.querySelectorAll('[data-camera],#reset').forEach(b=>b.addEventListener('click',()=>stop(),true));
 const allTargets=[...(D.nodules||[]),...(D.review_candidates||[])];const requested=new URLSearchParams(location.hash.slice(1)).get('review');if(requested){const target=allTargets.find(n=>n.id===requested),p=target?.center_ras_mm;if(p){$('#target').value=requested;state.point=[-p[0],-p[1],p[2]];draw()}}
 sync();return{sync,marker,stop,rotationCenter,get playing(){return playing}};
};

'use strict';
// Record only rendered 3D frames in the browser; no CT or video is uploaded.
window.createThreeVideoRecorder=function({renderer,renderController,download}){
 const button=document.querySelector('#record-three'),status=document.querySelector('#record-status');
 const canvas=document.createElement('canvas'),limitMs=5*60*1000,limitBytes=256*1024**2;
 let recorder=null,stream=null,track=null,chunks=[],bytes=0,started=0,reason='',discard=false;
 function label(active){button.textContent=active?'停止并保存':'录制三维';button.setAttribute('aria-pressed',String(active))}
 function release(){if(stream)for(const t of stream.getTracks())t.stop();stream=track=null}
 function stop(message=''){
  if(!recorder||recorder.state==='inactive')return;
  reason=message;button.disabled=true;renderController.setCapture(null);recorder.stop();
 }
 if(renderer.disabled||typeof MediaRecorder==='undefined'||typeof canvas.captureStream!=='function'){
  button.disabled=true;button.title='当前浏览器不支持三维视频录制';return{stop,get recording(){return false}};
 }
 button.disabled=false;
 button.onclick=()=>{
  if(recorder&&recorder.state!=='inactive'){stop();return}
  if(document.hidden){status.textContent='请先显示预览页面';return}
  try{
   const source=renderer.domElement,w=source.width,h=source.height;
   if(!w||!h)throw Error('三维视窗尚未就绪');
   const scale=Math.min(1,1920/w,1080/h);canvas.width=Math.max(2,Math.floor(w*scale/2)*2);canvas.height=Math.max(2,Math.floor(h*scale/2)*2);
   const context=canvas.getContext('2d',{alpha:false});if(!context)throw Error('无法创建录制画面');
   try{stream=canvas.captureStream(0);track=stream.getVideoTracks()[0];if(!track||typeof track.requestFrame!=='function'){release();stream=canvas.captureStream(30);track=stream.getVideoTracks()[0]}}
   catch(error){release();stream=canvas.captureStream(30);track=stream.getVideoTracks()[0]}
   if(!track)throw Error('无法创建视频轨道');
   const candidates=['video/webm;codecs=vp9','video/webm;codecs=vp8','video/webm'];
   const mime=typeof MediaRecorder.isTypeSupported==='function'?candidates.find(type=>MediaRecorder.isTypeSupported(type)):undefined;
   recorder=new MediaRecorder(stream,{...(mime?{mimeType:mime}:{}),videoBitsPerSecond:4000000});
   chunks=[];bytes=0;reason='';discard=false;started=performance.now();
   recorder.ondataavailable=event=>{if(event.data?.size){chunks.push(event.data);bytes+=event.data.size;if(bytes>=limitBytes)stop('已达到录制大小上限')}};
   recorder.onerror=()=>{reason='录制中断';renderController.setCapture(null);if(recorder.state!=='inactive')stop(reason)};
   recorder.onstop=()=>{
    renderController.setCapture(null);release();label(false);button.disabled=false;
    if(discard){chunks=[];status.textContent='';return}
    const type=recorder.mimeType||mime||'video/webm',blob=new Blob(chunks,{type});chunks=[];
    if(!blob.size){status.textContent=reason||'未生成视频，请重试';return}
    try{download(blob,'three-dimensional-view.'+(type.startsWith('video/mp4')?'mp4':'webm'));status.textContent=reason||'视频已保存'}
    catch(error){status.textContent='视频保存失败，请重试'}
   };
   recorder.start(1000);label(true);status.textContent='录制中 00:00';
   renderController.setCapture(frame=>{
    try{
     const fit=Math.min(canvas.width/frame.width,canvas.height/frame.height),width=frame.width*fit,height=frame.height*fit;
     context.fillStyle='#111113';context.fillRect(0,0,canvas.width,canvas.height);context.drawImage(frame,(canvas.width-width)/2,(canvas.height-height)/2,width,height);
     track?.requestFrame?.();const elapsed=Math.max(0,Math.floor((performance.now()-started)/1000));
     status.textContent='录制中 '+String(Math.floor(elapsed/60)).padStart(2,'0')+':'+String(elapsed%60).padStart(2,'0');
     if(elapsed*1000>=limitMs)stop('已达到录制时长上限');
    }catch(error){stop('录制中断')}
   });
  }catch(error){renderController.setCapture(null);release();recorder=null;label(false);button.disabled=false;status.textContent=error.message||'无法开始录制'}
 };
 document.addEventListener('visibilitychange',()=>{if(document.hidden)stop('页面切换，录制已结束')});
 document.addEventListener('viewer-three-paused',()=>stop('三维显示中断，录制已结束'));
 window.addEventListener('pagehide',()=>{discard=true;stop();release()});
 return{stop,get recording(){return !!recorder&&recorder.state!=='inactive'}};
};
/* A4 image sheet, with patient details kept only in this page. */
window.createViewerPrintComposer=function({renderer,renderController}){
 const q=s=>document.querySelector(s),open=q('#print-open'),panel=q('#print-composer'),sheet=q('#print-sheet'),views=q('#print-views'),add=q('#print-add'),output=q('#print-output'),message=q('#print-message'),count=q('#print-count');
 const items=[];let logoURL=null;
 const element=(tag,klass,text)=>{const node=document.createElement(tag);if(klass)node.className=klass;if(text!==undefined)node.textContent=text;return node};
 const decoded=image=>typeof image.decode==='function'?image.decode():new Promise((resolve,reject)=>{if(image.complete)return image.naturalWidth?resolve():reject(Error('图片读取失败'));image.onload=resolve;image.onerror=reject});
 function sync(){
  views.replaceChildren();count.textContent=items.length+' / 9';add.disabled=items.length>=9||!!renderer.disabled;output.disabled=!items.length;
  items.forEach((item,index)=>{
   const row=element('div','print-view'),img=element('img');img.src=item.url;img.alt='视角 '+(index+1);const label=element('input');label.type='text';label.maxLength=32;label.value=item.label;label.setAttribute('aria-label','视角名称 '+(index+1));label.oninput=()=>{item.label=label.value};
   const up=element('button',null,'上移'),down=element('button',null,'下移'),remove=element('button',null,'移除');up.type=down.type=remove.type='button';up.disabled=index===0;down.disabled=index===items.length-1;
   up.onclick=()=>{[items[index-1],items[index]]=[items[index],items[index-1]];sync()};down.onclick=()=>{[items[index+1],items[index]]=[items[index],items[index+1]];sync()};remove.onclick=()=>{URL.revokeObjectURL(item.url);items.splice(index,1);sync()};
   const controls=element('div','print-view-actions');controls.append(up,down,remove);const detail=element('div','print-view-detail');detail.append(label,controls);row.append(img,detail);views.append(row);
  });
 }
 async function capture(){
  if(items.length>=9||renderer.disabled)return;add.disabled=true;message.textContent='';
  try{
   renderController.renderNow();const blob=await new Promise(resolve=>renderer.domElement.toBlob(resolve,'image/png'));
   if(!blob||!blob.size)throw Error('截图失败，请重试');items.push({url:URL.createObjectURL(blob),label:'视角 '+(items.length+1)});sync();
  }catch(error){message.textContent=error.message||'截图失败';sync()}
 }
 function build(){
  sheet.replaceChildren();const header=element('header','print-sheet-header'),identity=element('div','print-sheet-identity');
  if(logoURL){const logo=element('img','print-sheet-logo');logo.src=logoURL;logo.alt='机构标识';header.append(logo)}
  identity.append(element('div','print-sheet-institution',q('#print-institution').value.trim()),element('h1',null,'三维重建影像'));
  header.append(identity);sheet.append(header);
  const patient=element('div','print-sheet-patient');for(const [label,id] of [['姓名','#print-patient'],['性别','#print-sex'],['年龄','#print-age'],['检查号','#print-id'],['检查日期','#print-date']]){
   const part=element('div','print-sheet-patient-field'),name=element('span','print-sheet-label',label+'：'),value=element('span','print-sheet-value',q(id).value.trim()||'');part.append(name,value);patient.append(part);
  }
  sheet.append(patient);const grid=element('div','print-sheet-grid');grid.dataset.count=String(items.length);
  for(const item of items){const figure=element('figure','print-sheet-view'),img=element('img');img.src=item.url;img.alt=item.label||'三维视角';figure.append(img,element('figcaption',null,item.label.trim()));grid.append(figure)}sheet.append(grid);return [...sheet.querySelectorAll('img')];
 }
 open.disabled=false;open.onclick=()=>{panel.hidden=!panel.hidden;if(!panel.hidden)message.textContent=''};q('#print-close').onclick=()=>{panel.hidden=true};add.onclick=capture;
 q('#print-logo').onchange=async event=>{const file=event.target.files?.[0];if(!file){if(logoURL)URL.revokeObjectURL(logoURL);logoURL=null;return}if(!['image/png','image/jpeg','image/webp'].includes(file.type)||file.size>5*1024**2){if(logoURL)URL.revokeObjectURL(logoURL);logoURL=null;message.textContent='请选择不超过 5 MB 的 PNG、JPEG 或 WebP 图片';event.target.value='';return}const url=URL.createObjectURL(file);try{const image=new Image();image.src=url;await decoded(image);if(logoURL)URL.revokeObjectURL(logoURL);logoURL=url;message.textContent=''}catch{URL.revokeObjectURL(url);if(logoURL)URL.revokeObjectURL(logoURL);logoURL=null;event.target.value='';message.textContent='标识图片无法读取'}};
 output.onclick=async()=>{if(!items.length)return;output.disabled=true;message.textContent='';try{const images=build();await Promise.all(images.map(decoded));sheet.hidden=false;document.body.classList.add('print-ready');window.print()}catch(error){sheet.hidden=true;document.body.classList.remove('print-ready');message.textContent='无法准备打印页面，请重试'}finally{output.disabled=!items.length}};
 window.addEventListener('afterprint',()=>{sheet.hidden=true;document.body.classList.remove('print-ready')});window.addEventListener('pagehide',()=>{for(const item of items)URL.revokeObjectURL(item.url);if(logoURL)URL.revokeObjectURL(logoURL)});
 sync();return{capture,get count(){return items.length}};
};
