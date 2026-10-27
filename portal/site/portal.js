"use strict";
const search=document.querySelector('#search'),entries=[...document.querySelectorAll('.entry')];
search.addEventListener('input',()=>{const term=search.value.trim().toLocaleLowerCase();let visible=0;for(const entry of entries){entry.hidden=!entry.dataset.search.includes(term);if(!entry.hidden)visible++}const empty=document.querySelector('#empty');empty.hidden=visible!==0;empty.textContent=entries.length?'没有匹配的结果':'暂无重建结果';document.querySelector('.list-head').hidden=entries.length===0});
search.dispatchEvent(new Event('input'));
let timer;
function notice(message){const box=document.querySelector('#notice');box.textContent=message;box.style.display='block';clearTimeout(timer);timer=setTimeout(()=>box.style.display='none',2800)}
document.addEventListener('click',async event=>{const button=event.target.closest('[data-share]');if(!button)return;const url=new URL(button.dataset.share,location.origin).href;if(window.isSecureContext&&navigator.clipboard){try{await navigator.clipboard.writeText(url);notice('链接已复制');return}catch{}}const input=document.querySelector('#share-url');input.value=url;document.querySelector('#share-dialog').showModal();input.focus();input.select();try{if(document.execCommand('copy'))notice('链接已复制；也可手动复制下方地址')}catch{}});
document.querySelector('#close-share').addEventListener('click',()=>document.querySelector('#share-dialog').close());

// Workbench previews are linked in place; no duplicate 300+ MB publication copy.
fetch('/api/jobs').then(r=>{if(!r.ok)throw Error('工作台暂不可用');return r.json()}).then(data=>{
 const fragment=document.createDocumentFragment();
 for(const job of data.jobs.filter(j=>j.preview&&/^[a-f0-9]{32}$/.test(j.id))){
  const url='/results/'+job.id+'/preview.html',title=job.note_name||'CT 检查 '+job.id.slice(0,8),date=new Date((job.updated_at||job.created_at)*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'});
  const row=document.createElement('article');row.className='entry';row.dataset.search=(title+' '+date+' '+job.id).toLowerCase();
  const content=document.createElement('div'),h=document.createElement('h2'),p=document.createElement('p'),time=document.createElement('div'),actions=document.createElement('div');
  h.textContent=title;p.textContent=job.data_pruned?'已归档预览':(job.mode==='full'?'精细重建':'快速重建')+' / 服务器离线计算';content.append(h,p);time.className='date';time.textContent=date;actions.className='actions';
  for(const [name,kind] of [['打开预览','open'],['复制链接','share'],['下载离线 HTML','download']]){const control=document.createElement(kind==='share'?'button':'a');control.textContent=name;if(kind==='share'){control.type='button';control.dataset.share=url}else{control.href=url;if(kind==='open')control.className='primary';else{control.className='download';control.href=url+'?download=1';control.download='thorax-'+job.id.slice(0,8)+'.html'}}actions.append(control)}
  row.append(content,time,actions);entries.push(row);fragment.append(row);
 }
 document.querySelector('#results').prepend(fragment);document.querySelector('.count').textContent='共 '+entries.length+' 套结果';search.dispatchEvent(new Event('input'));
}).catch(()=>{});
