'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const THREE=require('../assets/three.min.js');
const context=vm.createContext({THREE});context.window=context;
vm.runInContext(fs.readFileSync(path.join(__dirname,'../assets/viewer-interactions.js'),'utf8'),context);
const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(36,1,.1,10000);
const lighting=context.createViewerLighting({THREE,scene,camera}),main=lighting.lights[0];
function direction(light){return light.getWorldPosition(new THREE.Vector3()).sub(light.target.getWorldPosition(new THREE.Vector3())).normalize()}
let poses=0,minGain=Infinity,maxGain=0,minContrast=Infinity;
for(const pivot of [new THREE.Vector3(),new THREE.Vector3(90,-110,40)]){
 for(const offset of [[0,500,0],[500,0,0],[0,-500,0],[-500,0,0],[100,200,500],[100,200,-500]]){
  camera.up.set(0,0,1);camera.position.copy(pivot).add(new THREE.Vector3(...offset));camera.lookAt(pivot);scene.updateMatrixWorld(true);
  const observerDirection=direction(main).applyQuaternion(camera.quaternion.clone().invert());
  assert.ok(observerDirection.x<-.3&&observerDirection.y>.4&&observerDirection.z>.6,'key light left the upper-left viewing quadrant');
  for(const light of lighting.lights){assert.equal(light.parent,camera);assert.equal(light.castShadow,false)}
  const gain=normal=>lighting.ambient.intensity+lighting.lights.reduce((sum,light)=>sum+light.intensity*Math.max(0,normal.dot(direction(light))),0);
  const upperLeft=new THREE.Vector3(-1,1,1).normalize().applyQuaternion(camera.quaternion),lowerRight=new THREE.Vector3(1,-1,1).normalize().applyQuaternion(camera.quaternion);
  const contrast=gain(upperLeft)/gain(lowerRight);minContrast=Math.min(minContrast,contrast);assert.ok(contrast>1.5,'view-facing curvature lost its directional contrast');
  // Uniform fill must keep anatomy visible without flattening directional shape.
  for(const normal of [new THREE.Vector3(1,0,0),new THREE.Vector3(0,1,0),new THREE.Vector3(0,0,1)]){
   const value=gain(normal);minGain=Math.min(minGain,value);maxGain=Math.max(maxGain,value);
  }
  poses++;
 }
}
assert.ok(minGain>=.4,'unlit anatomy lost its fill');assert.ok(maxGain/minGain>1.5&&maxGain/minGain<3,'directional shape is flat or excessively dark');
console.log(JSON.stringify({camera_relative:true,upper_left:true,shadow_casting:false,poses,min_gain:minGain,max_gain:maxGain,min_contrast:minContrast}));
