'use strict';
const assert=require('node:assert'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
global.window=global;
vm.runInThisContext(fs.readFileSync(path.join(__dirname,'../assets/native-volume.js'),'utf8'));
for(const budget of [64,128].map(x=>x*1024**2)){
  const plan=planCTVolumeSampling([512,512,538],[429,292,471],2,budget,2048);
  assert(plan.bytes<=budget);
  assert(plan.ctStride.some(n=>n>1)||plan.roiStride.some(n=>n>1));
  assert(plan.ctStride.every(n=>Number.isInteger(n)&&n>=1&&n<=32));
  assert(plan.roiStride.every(n=>Number.isInteger(n)&&n>=1&&n<=32));
}
const native=planCTVolumeSampling([64,64,64],[32,64,64],2,128*1024**2,2048);
assert.deepEqual(native.ctStride,[1,1,1]);assert.deepEqual(native.roiStride,[1,1,1]);
const limited=planCTVolumeSampling([1024,512,538],null,2,128*1024**2,256);
assert(limited.ctStride.every((n,k)=>Math.ceil([1024,512,538][k]/n)<=256));
assert.throws(()=>planCTVolumeSampling([100,100,100],null,2,0,2048));
console.log('CT and ROI sampling plans respect budget and texture limits');
