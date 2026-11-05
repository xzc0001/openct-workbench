const assert=require('assert'),fs=require('fs'),vm=require('vm'),path=require('path');
const code=fs.readFileSync(path.join(__dirname,'../assets/viewer-interactions.js'),'utf8');
function fixture({supported=true,manual=true}={}){
 let capture=null,now=0,downloads=[],draws=[],streams=[],events={},windowEvents={},instances=[];
 const button={disabled:true,setAttribute(k,v){this[k]=v}},status={};
 const canvas={width:0,height:0,getContext(){return {fillRect(){},drawImage(...x){draws.push(x)}}},captureStream(fps){const track={stopped:false,stop(){this.stopped=true}};if(manual)track.requestFrame=()=>{track.frames=(track.frames||0)+1};const stream={fps,track,getTracks(){return [track]},getVideoTracks(){return [track]}};streams.push(stream);return stream}};
 class Recorder{static isTypeSupported(t){return t.includes('vp8')}constructor(stream,opts){this.stream=stream;this.mimeType=opts.mimeType;this.state='inactive';instances.push(this)}start(){this.state='recording'}stop(){this.state='inactive';this.ondataavailable({data:new Blob(['video'])});this.onstop()}}
 const doc={hidden:false,querySelector(q){return q==='#record-three'?button:status},createElement(){return canvas},addEventListener(k,v){events[k]=v}};
 const context={document:doc,Blob,performance:{now:()=>now},MediaRecorder:supported?Recorder:undefined,console};context.window=context;context.addEventListener=(k,v)=>windowEvents[k]=v;vm.runInNewContext(code,context);
 const renderer={domElement:{width:2400,height:1200}},controller={setCapture(v){capture=v}};
 const api=context.createThreeVideoRecorder({renderer,renderController:controller,download:(blob,name)=>downloads.push({blob,name})});
 return {button,status,canvas,renderer,api,streams,draws,downloads,events,doc,instances,frame(){assert(capture);capture(renderer.domElement)},get capture(){return capture},set now(v){now=v}};
}
{
 const f=fixture();assert.equal(f.button.disabled,false);f.button.onclick();assert(f.api.recording);assert.equal(f.canvas.width,1920);assert.equal(f.canvas.height,960);f.frame();assert.strictEqual(f.draws[0][0],f.renderer.domElement);assert.equal(f.streams[0].track.frames,1);f.button.onclick();assert(!f.api.recording);assert.equal(f.capture,null);assert(f.streams[0].track.stopped);assert.equal(f.downloads.length,1);assert.equal(f.downloads[0].name,'three-dimensional-view.webm');assert(f.downloads[0].blob.size>0);
}
{
 const f=fixture({manual:false});f.button.onclick();assert.deepEqual(f.streams.map(s=>s.fps),[0,30]);assert(f.streams[0].track.stopped);f.doc.hidden=true;f.events.visibilitychange();assert.equal(f.capture,null);assert.equal(f.downloads.length,1);
}
{
 const f=fixture();f.button.onclick();f.now=301000;f.frame();assert.equal(f.capture,null);assert.equal(f.status.textContent,'已达到录制时长上限');assert.equal(f.downloads.length,1);
}
{const f=fixture({supported:false});assert(f.button.disabled);assert(!f.api.recording)}
console.log('video recorder simulated lifecycle: OK');
