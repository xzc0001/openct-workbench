'use strict';
(()=>{
 const panel=document.querySelector('#storage-panel'),q=s=>document.querySelector(s);
 const bytes=n=>n>=1024**3?(n/1024**3).toFixed(2)+' GiB':(n/1024**2).toFixed(1)+' MiB';
 let loaded=false,currentPlan=null,loading=false;
 async function load(){if(loading)return;loading=true;q('#storage-refresh').disabled=true;try{
  const data=await api('/api/storage');loaded=true;q('#storage-cache').hidden=!data.can_clean_cache;q('#storage-plan').hidden=account.role==='viewer';q('.cleanup-options').hidden=account.role==='viewer';q('#storage-status').textContent='';
  q('#storage-summary').textContent=`磁盘可用 ${bytes(data.disk_free)} / ${bytes(data.disk_total)} / 检查 ${bytes(data.jobs_bytes)} / 可再生缓存 ${bytes(data.cache_bytes)} / 模型权重 ${bytes(data.weights_bytes)}（保留）`;
  q('#storage-summary').classList.toggle('storage-low',data.disk_free<6*1024**3);
  q('#storage-tasks').innerHTML=data.jobs.length?`<div class=storage-table><table><thead><tr><th>选择</th><th>检查</th><th>状态</th><th>总占用</th><th>临时数据可释放</th></tr></thead><tbody>${data.jobs.map(j=>`<tr><td><input type=checkbox class=cleanup-job value="${j.id}" aria-label="选择检查 ${j.id.slice(0,8)}" ${j.can_clean?'':'disabled'}></td><td>${esc(j.note_name||j.id.slice(0,8))}<small>${j.id.slice(0,8)} / ${new Date(j.created_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}${j.source_job?' / 源 '+esc(j.source_job.slice(0,8)):''}</small></td><td>${j.data_pruned?'仅保留预览':esc(labels[j.state]||j.state)}</td><td>${bytes(j.bytes)}</td><td>${bytes(j.temporary_bytes)}</td></tr>`).join('')}</tbody></table></div>`:'<p class=hint>暂无检查数据。</p>';
 }catch(e){q('#storage-status').textContent=e.message}finally{loading=false;q('#storage-refresh').disabled=false}}
 panel.addEventListener('toggle',()=>{if(panel.open&&!loaded)load()});
 q('#storage-link').onclick=()=>{panel.open=true;if(!loaded)load()};
 q('#storage-refresh').onclick=load;
 const dialog=document.createElement('dialog');dialog.id='cleanup-dialog';dialog.innerHTML='<h2>确认清理范围</h2><p id="cleanup-description"></p><p id="cleanup-size"></p><p id="cleanup-jobs"></p><p id="cleanup-source-row">此操作将删除影像或重建数据，无法恢复。</p><div class="storage-toolbar"><button id="cleanup-execute" type="button">确认清理</button><button id="cleanup-cancel" type="button">取消</button></div>';
 document.body.append(dialog);
 async function preview(action,job_ids=[]){q('#storage-status').textContent='正在统计清理范围…';try{
  currentPlan=await api('/api/storage/plan',{action,job_ids});
  q('#cleanup-description').textContent=currentPlan.description;
  q('#cleanup-size').textContent='预计释放 '+bytes(currentPlan.bytes);
  q('#cleanup-jobs').textContent=currentPlan.jobs.length?'检查：'+currentPlan.jobs.map(j=>j.id.slice(0,8)+'（'+bytes(j.bytes)+'）').join('、'):'范围：可再生成的显示缓存与临时计算目录。模型权重保留。';
  q('#cleanup-source-row').hidden=!currentPlan.removes_source;q('#cleanup-execute').disabled=false;
  q('#storage-status').textContent='';dialog.showModal();
 }catch(e){q('#storage-status').textContent=e.message}}
 q('#storage-plan').onclick=()=>preview(q('input[name=cleanup-action]:checked').value,[...document.querySelectorAll('.cleanup-job:checked')].map(e=>e.value));
 q('#storage-cache').onclick=()=>preview('cache');
 q('#cleanup-cancel').onclick=()=>{currentPlan=null;dialog.close()};
 q('#cleanup-execute').onclick=async()=>{if(!currentPlan)return;const button=q('#cleanup-execute');button.disabled=true;try{
  const result=await api('/api/storage/execute',{token:currentPlan.token,confirmed:true});currentPlan=null;dialog.close();await load();last='';await refresh();q('#storage-status').textContent='清理完成，已移除 '+bytes(result.removed_bytes)+' 数据。';
 }catch(e){dialog.close();q('#storage-status').textContent=e.message;await load()}finally{button.disabled=false}};
 if(location.hash==='#storage-section')panel.open=true;
})();
