'use strict';
// Browser session and CSRF only. Devices never receive these capabilities.
(()=>{
 const button=document.createElement('button');button.className='text-button';button.textContent='计算节点';button.hidden=true;document.querySelector('nav').prepend(button);
 const dialog=document.createElement('dialog');dialog.id='cluster-dialog';
 dialog.innerHTML='<div class="dialog-heading"><h2>计算节点</h2><button data-close type="button">关闭</button></div><form id="cluster-settings"><label><input name="enabled" type="checkbox"> 启用节点调度</label><label><input name="local_fallback" type="checkbox"> 允许服务器计算</label><button type="submit">保存</button></form><div class="cluster-toolbar"><button id="cluster-refresh">刷新</button></div><div id="cluster-nodes"></div><p id="cluster-message" role="status"></p>';
 document.body.append(dialog);
 const q=s=>dialog.querySelector(s),names={ready:'可用',busy:'计算中',paused:'暂停',disabled:'已停用',draining:'完成当前任务后暂停',pending:'待批准',pending_approval:'待批准',pending_signing:'正在签发',provisioning:'正在签发',unsupported:'系统不支持',error:'错误',revoked:'已撤销',offline:'离线'};
 let timer;
 async function load(){const value=await api('/api/cluster/v1/admin/nodes');for(const k of ['enabled','local_fallback'])q('[name='+k+']').checked=!!value.settings[k];const nodes=[...(value.enrollments||[]),...(value.nodes||[])];q('#cluster-nodes').innerHTML=nodes.map(n=>{const state=n.state||n.status,registered=!!n.node_id&&!n.enrollment_id,resources=n.capabilities?.resources;return `<div class="cluster-node"><div><strong>${esc(n.display_name||'计算节点')}</strong><small>${esc(names[state]||state||'等待连接')}</small>${resources?`<small>CPU ${esc(resources.cpu_percent)}% / 内存 ${(resources.memory_bytes/1024**3).toFixed(1)} GiB</small>`:''}<small>${esc((n.node_id||n.enrollment_id||'').slice(0,8))}</small></div><div class="cluster-toolbar">${n.enrollment_id&&['pending','pending_approval'].includes(state)?`<button data-approve="${esc(n.enrollment_id)}">批准</button>`:registered&&state!=='revoked'?`<button data-${state==='disabled'?'enable':'disable'}="${esc(n.node_id)}">${state==='disabled'?'恢复':'停用'}</button><button class="text-button" data-revoke="${esc(n.node_id)}">撤销证书</button>`:''}</div></div>`}).join('')||'<p class="empty">还没有计算节点</p>'}
 async function action(fn){try{await fn();q('#cluster-message').textContent='已更新';await load()}catch(e){q('#cluster-message').textContent=e.message}}
 sessionReady.then(user=>{button.hidden=user.role!=='admin'}).catch(()=>{});
 button.onclick=()=>{dialog.showModal();action(load);timer=setInterval(()=>load().catch(e=>q('#cluster-message').textContent=e.message),5000)};
 q('[data-close]').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{clearInterval(timer)});
 q('#cluster-refresh').onclick=()=>action(load);
 q('#cluster-settings').onsubmit=e=>{e.preventDefault();action(()=>api('/api/cluster/v1/admin/settings',{enabled:q('[name=enabled]').checked,local_fallback:q('[name=local_fallback]').checked}))};
 q('#cluster-nodes').onclick=e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.approve)action(()=>api('/api/cluster/v1/admin/approve',{enrollment_id:b.dataset.approve}));else for(const key of ['disable','enable','revoke'])if(b.dataset[key]){action(()=>api('/api/cluster/v1/admin/'+key,{node_id:b.dataset[key]}));break}};
})();
