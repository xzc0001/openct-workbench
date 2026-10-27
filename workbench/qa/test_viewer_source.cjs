'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
(async()=>{
 const data=JSON.parse(fs.readFileSync(0,'utf8'));global.window=global;
 global.document={getElementById:id=>({textContent:data.assets[id.replace('asset-','')]})};
 if(data.onlineBase){data.ct.transport={base:data.onlineBase};global.DecompressionStream=undefined}
 else global.fetch=()=>{throw Error('Offline sampler attempted a network request')};
 let failedRequests=0;
 if(data.requestHeaders){const actualFetch=global.fetch;global.fetch=(url,options)=>actualFetch(url,{...options,headers:{...options?.headers,...data.requestHeaders}})}
 if(data.failFirstRequests){const actualFetch=global.fetch;global.fetch=(url,options)=>{if(failedRequests<data.failFirstRequests){failedRequests++;return Promise.reject(new TypeError('Failed to fetch'))}return actualFetch(url,options)}}
 vm.runInThisContext(fs.readFileSync(path.join(__dirname,'../assets/viewer-source.js'),'utf8'));
 const source=await createViewerSource(data.ct,data.rois),plane=await source.plane(data.plane),volume=await source.region('',128*1024**2);
 const hash=a=>crypto.createHash('sha256').update(new Uint8Array(a.buffer,a.byteOffset,a.byteLength)).digest('hex');
 const result={plane_sha256:hash(plane),volume_sha256:hash(volume.data),cached_bytes:source.cachedBytes,failed_requests:failedRequests};
 if(data.model)result.model_sha256=hash(new Uint8Array(await source.load(data.model.positions)));
 process.stdout.write(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exit(1)});
