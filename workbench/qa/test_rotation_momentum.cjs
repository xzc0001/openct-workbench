'use strict';
// Use the shipped OrbitControls and real camera math; only DOM and GPU calls
// are replaced. Verify actual pointer input and that drawing returns to idle.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const THREE=require('../assets/three.min.js');
class Element{
 constructor(){this.listeners=new Map();this.checked=false;this.hidden=false;this.style={};this.clientWidth=1200;this.clientHeight=800}
 addEventListener(type,listener){if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type).add(listener)}
 removeEventListener(type,listener){this.listeners.get(type)?.delete(listener)}
 dispatchEvent(event){for(const listener of [...(this.listeners.get(event.type)||[])])listener(event)}
 setAttribute(){} focus(){}
}
function setup(storage=new Map()){
 const elements=new Map(),document=new Element(),canvas=new Element(),raf=new Map(),timers=new Map();let clock=1000,nextId=0,draws=0,adaptive=false;
 document.hidden=false;document.querySelector=key=>{if(!elements.has(key))elements.set(key,new Element());return elements.get(key)};
 document.querySelector('#interaction-quality').checked=true;document.querySelector('#interaction-context').checked=true;
 canvas.ownerDocument=document;
 const context=vm.createContext({THREE,document,console,devicePixelRatio:1,performance:{now:()=>clock},Event:class{constructor(type){this.type=type}},localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value)},requestAnimationFrame:fn=>{raf.set(++nextId,fn);return nextId},cancelAnimationFrame:id=>raf.delete(id),setTimeout:(fn,delay)=>{timers.set(++nextId,{fn,time:clock+delay});return nextId},clearTimeout:id=>timers.delete(id)});
 context.window=context;
 for(const file of ['OrbitControls.js','render-performance.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../assets',file),'utf8'),context);
 const camera=new THREE.PerspectiveCamera(36,1,.1,10000);camera.up.set(0,0,1);camera.position.set(0,400,100);
 const controls=new THREE.OrbitControls(camera,canvas),renderer={domElement:canvas,debug:{},info:{render:{triangles:0}},setPixelRatio(ratio){this.ratio=ratio},setSize(width,height){canvas.width=width*this.ratio;canvas.height=height*this.ratio},getContext:()=>({getError:()=>0,NO_ERROR:0,isContextLost:()=>false}),render(){draws++}},volumeRendering={active:true,beforeRender(value){adaptive=value}};
 const controller=context.createViewerRenderController({renderer,scene:{},camera,controls,meshes:new Map(),state:{initialQuality:'full'},geometryFrom(){},prepareGeometry:async()=>{},modelGroup:{visible:true},volumeRendering});
 function step(ms=1000/60){clock+=ms;for(const [id,item] of [...timers])if(item.time<=clock){timers.delete(id);item.fn()}for(const [id,fn] of [...raf]){raf.delete(id);fn(clock)}}
 function idle(max=250){let frames=0;while(raf.size||timers.size){assert.ok(frames++<max,'render loop did not return to idle');step()}return frames}
 function emit(target,type,values={}){target.dispatchEvent({type,pointerType:'mouse',button:0,clientX:200,clientY:200,preventDefault(){},...values})}
 function drag(){emit(canvas,'pointerdown');for(let i=1;i<=5;i++){emit(document,'pointermove',{clientX:200+i*22,clientY:200+i*4});step()}emit(document,'pointerup')}
 function toggle(value){const input=document.querySelector('#rotation-momentum');input.checked=value;input.onchange()}
 idle();return{camera,controls,controller,document,canvas,drag,toggle,step,idle,storage,get draws(){return draws},get adaptive(){return adaptive},get pending(){return raf.size}};
}
const test=setup();
assert.equal(test.controls.enableDamping,false,'momentum must default off');
test.drag();let released=test.camera.position.clone();test.idle();assert.ok(test.camera.position.distanceTo(released)<1e-7,'disabled momentum moved after release');
let idleDraws=test.draws;for(let i=0;i<20;i++)test.step();assert.equal(test.draws,idleDraws,'static view kept drawing');
test.toggle(true);test.drag();released=test.camera.position.clone();test.step();assert.ok(test.camera.position.distanceTo(released)>.001,'enabled momentum did not coast');assert.equal(test.controller.interacting,true);assert.equal(test.adaptive,true,'coasting must retain interactive quality');
const refine=test.document.querySelector('#interaction-quality');refine.checked=false;refine.onchange();test.step();assert.equal(test.adaptive,false,'quality opt-out must also apply to coasting');
test.idle();assert.equal(test.controller.interacting,false,'quality did not return after coasting');
idleDraws=test.draws;for(let i=0;i<20;i++)test.step();assert.equal(test.draws,idleDraws,'settled momentum kept drawing');
test.drag();test.step();released=test.camera.position.clone();test.toggle(false);assert.ok(test.camera.position.distanceTo(released)<1e-7,'disabling momentum jumped the camera');test.idle();assert.ok(test.camera.position.distanceTo(released)<1e-7,'disabled momentum kept moving');
test.toggle(true);test.drag();test.step();released=test.camera.position.clone();test.controller.stopMomentum();assert.ok(test.camera.position.distanceTo(released)<1e-7,'stopping momentum jumped the camera');test.idle();
test.controller.setAnimation(seconds=>{test.camera.position.applyAxisAngle(new THREE.Vector3(0,0,1),seconds);test.controls.update()});assert.equal(test.controls.enableDamping,false,'autoplay must not inherit damping deltas');for(let i=0;i<8;i++)test.step();test.controller.setAnimation(null);released=test.camera.position.clone();test.idle();assert.ok(test.camera.position.distanceTo(released)<1e-7,'autoplay pause kept moving');assert.equal(test.controls.enableDamping,true,'selected momentum was not restored after autoplay');
test.drag();test.step();test.document.hidden=true;test.document.dispatchEvent({type:'visibilitychange'});released=test.camera.position.clone();for(let i=0;i<20;i++)test.step();assert.equal(test.pending,0,'hidden page kept drawing');test.document.hidden=false;test.document.dispatchEvent({type:'visibilitychange'});test.idle();assert.ok(test.camera.position.distanceTo(released)<1e-7,'hidden momentum resumed unexpectedly');
const reopened=setup(test.storage);assert.equal(reopened.controls.enableDamping,true,'saved momentum choice was not restored');
// Slow frames must still settle; damping is tied to elapsed time during coast.
reopened.drag();let slowFrames=0;while(reopened.pending){assert.ok(slowFrames++<50,'slow renderer kept coasting');reopened.step(100)}
console.log(JSON.stringify({default_off:true,coasts_and_stops:true,idle_without_redraw:true,no_jump_when_disabled:true,quality_opt_out:true,autoplay_pause:true,visibility_pause:true,persisted:true,slow_frames:slowFrames}));
