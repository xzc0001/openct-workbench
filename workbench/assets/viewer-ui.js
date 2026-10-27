'use strict';
// Radix UI Colors 3.0. Display colors do not change CT or segmentation data.
window.ViewerUI=(()=>{
 const palette={slate1:'#fcfcfd',darkSlate1:'#111113',darkSlate11:'#b0b4ba',blue9:'#0090ff',red9:'#e5484d',amber4:'#ffee9c',amber9:'#ffc53d',lime9:'#bdee63',teal7:'#83cdc1',teal8:'#53b9ab',cyan8:'#3db9cf',pink8:'#dd93c2',violet8:'#aa99ec',violet9:'#6e56cf',grass8:'#65ba74',orange8:'#ec9455',orange9:'#f76b15'};
 const rgb=hex=>[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)/255);
 const groupNames={lungs:'肺轮廓',heart:'心脏',aorta:'主动脉',airways:'气道',arteries:'肺动脉',veins:'肺静脉',nodules:'结节候选',lobes:'肺叶',ribs:'肋骨',ribcage:'骨性胸廓',spine:'脊柱',scapulae:'肩胛骨',margin:'几何外扩参考',reviewed_regions:'已选疑似区域'};
 function cleanName(value){return String(value||'').replace(/[（(](?:自动标签|自动分割|主气道连通树)[）)]/g,'').replace(/\s*·\s*/g,'，').trim()}
 function modelColor(m){const lobes={lung_upper_lobe_left:'cyan8',lung_lower_lobe_left:'teal8',lung_upper_lobe_right:'grass8',lung_middle_lobe_right:'pink8',lung_lower_lobe_right:'violet8'};const groups={lungs:'teal7',lobes:'teal8',heart:'pink8',aorta:'orange9',airways:'lime9',arteries:'blue9',veins:'red9',nodules:'violet9',reviewed_regions:'violet9',margin:'orange8',ribs:'amber4',ribcage:'amber4',spine:'amber4',scapulae:'amber4'};return rgb(palette[lobes[m.id]||groups[m.group]||'cyan8'])}
 function mount({D,meshes,draw,invalidate}){
  const $=s=>document.querySelector(s),entries=[],pending=[],groups=new Map(),tabs=[...document.querySelectorAll('[data-controls]')];let query='';
  const expanded=new Map([...document.querySelectorAll('#layers details')].map(e=>[e.dataset.group,e.open]));$('#layers').replaceChildren();$('#models-status').hidden=true;
  function activate(tab){for(const t of tabs){const active=t===tab;t.setAttribute('aria-selected',String(active));t.tabIndex=active?0:-1;$('#controls-'+t.dataset.controls).hidden=!active}$('.control-content').scrollTop=0}
  tabs.forEach((tab,index)=>{tab.onclick=()=>activate(tab);tab.onkeydown=e=>{const keys={ArrowRight:(index+1)%tabs.length,ArrowLeft:(index+tabs.length-1)%tabs.length,Home:0,End:tabs.length-1};if(!(e.key in keys))return;e.preventDefault();const next=tabs[keys[e.key]];activate(next);next.focus()}});
  const eligible=items=>items.filter(e=>e.matches&&!e.check.disabled);
  function checkboxState(control,items){const list=eligible(items),checked=list.filter(e=>e.check.checked).length;control.disabled=!list.length;control.checked=!!list.length&&checked===list.length;control.indeterminate=checked>0&&checked<list.length;return[checked,list.length]}
  function syncLayers(){
   const [selected,total]=checkboxState($('#layer-all'),entries);$('#layer-count').textContent=selected+'/'+total;$('#layer-all-label').textContent=query?'搜索结果':'全部模型';
   for(const g of groups.values())if(g.check){const [on,count]=checkboxState(g.check,g.entries);g.count.textContent=on+'/'+count}
   for(const e of entries){e.slider.disabled=e.check.disabled||!e.check.checked;e.row.classList.toggle('is-hidden',!e.check.checked);e.row.classList.toggle('is-locked',e.check.disabled)}
  }
  function changed(){syncLayers();invalidate();draw()}
  function toggle(items,checked){for(const e of eligible(items)){e.check.checked=checked;meshes.get(e.model.id).visible=checked}changed()}
  const buckets=new Map();for(const model of D.models){if(!buckets.has(model.group))buckets.set(model.group,[]);buckets.get(model.group).push(model)}
  const plan=D.reconstruction_plan||D.stream?.plan||[],planned=new Map(plan.map(p=>[p.group,p]));
  for(const key of new Set([...plan.map(p=>p.group),...buckets.keys()])){
   const models=buckets.get(key)||[];
   if(!models.length){
    const item=planned.get(key);if(!item)continue;
    const row=document.createElement('div'),label=document.createElement('label'),check=document.createElement('input'),name=document.createElement('span'),status=document.createElement('span');
    row.className='layer model-placeholder';row.dataset.structure=item.id;row.dataset.phase=item.phase;check.type='checkbox';check.disabled=true;check.setAttribute('aria-label',item.label+'占位');name.textContent=item.label;
    status.className='placeholder-state';status.textContent=window.ReconstructionPhases?.[item.phase]||'待重建';label.append(check,name);row.append(label,status);$('#layers').append(row);pending.push({row,matches:true,search:item.label.toLocaleLowerCase()});continue;
   }
   const container=document.createElement(models.length>1?'details':'div'),group={container,entries:[],normalOpen:expanded.get(key)??models.length<=8};container.dataset.group=key;groups.set(key,group);
   if(models.length>1){container.className='model-group';container.open=group.normalOpen;const summary=document.createElement('summary'),label=document.createElement('label'),check=document.createElement('input'),name=document.createElement('span'),count=document.createElement('span');check.type='checkbox';check.setAttribute('aria-label','显示全部'+(groupNames[key]||cleanName(key)));name.textContent=groupNames[key]||cleanName(key);count.className='group-count';label.append(check,name);summary.append(label,count);container.append(summary);group.check=check;group.count=count;check.onchange=()=>toggle(group.entries,check.checked);container.ontoggle=()=>{if(!query)group.normalOpen=container.open}}
   $('#layers').append(container);
   for(const model of models){
    const mesh=meshes.get(model.id),row=document.createElement('div'),label=document.createElement('label'),check=document.createElement('input'),swatch=document.createElement('span'),name=document.createElement('span'),slider=document.createElement('input'),value=document.createElement('output'),title=cleanName(model.name)||groupNames[key]||key;
    row.className='layer';row.dataset.model=model.id;check.type='checkbox';check.checked=mesh.visible;check.disabled=key==='margin';check.dataset.layer=model.id;model.check=check;if(key==='margin')row.title='在定位中选择目标并核对后启用';
    swatch.className='model-swatch';swatch.style.backgroundColor='rgb('+model.color.map(v=>Math.round(v*255)).join(',')+')';swatch.setAttribute('aria-hidden','true');name.className='model-name';name.textContent=title;label.append(check,swatch,name);
    slider.type='range';slider.min=0;slider.max=100;slider.value=Math.round(model.opacity*100);slider.setAttribute('aria-label',title+'不透明度');value.textContent=slider.value+'%';row.append(label,slider,value);container.append(row);
    const entry={model,row,check,slider,value,matches:true,search:(title+' '+(groupNames[key]||key)).toLocaleLowerCase()};entries.push(entry);group.entries.push(entry);
    check.onchange=()=>{mesh.visible=check.checked;changed()};slider.oninput=()=>{const mat=mesh.material;mat.opacity=Number(slider.value)/100;model.opacity=mat.opacity;mat.transparent=mat.opacity<1;mat.depthWrite=mat.opacity>=1;mat.needsUpdate=true;value.textContent=slider.value+'%';changed()};
   }
  }
  $('#layer-all').onchange=()=>toggle(entries,$('#layer-all').checked);
  $('#layer-search').oninput=()=>{query=$('#layer-search').value.trim().toLocaleLowerCase();for(const e of [...entries,...pending]){e.matches=!query||e.search.includes(query);e.row.hidden=!e.matches}for(const g of groups.values()){g.container.hidden=!g.entries.some(e=>e.matches);if(g.check)g.container.open=query?true:g.normalOpen}$('#layer-empty').hidden=[...entries,...pending].some(e=>e.matches);syncLayers()};
  const missing=(D.missing_structures||[]).map(cleanName);if(missing.length){$('#models-status').textContent='未生成：'+missing.join('、');$('#models-status').hidden=false}
  function syncTargets(){const target=$('#target').value;$('#focus').disabled=!target;$('#margin-controls').hidden=!D.models.some(m=>m.group==='margin'&&m.target_id===target)}
  function syncWindow(){const ww=Number($('#ww').value),wl=Number($('#wl').value),presets={lung:[1500,-600],soft:[400,40],bone:[1800,400]};for(const b of document.querySelectorAll('[data-window]')){const p=presets[b.dataset.window],active=p[0]===ww&&p[1]===wl;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active))}}
  $('#layer-search').oninput();syncLayers();syncTargets();syncWindow();return{syncLayers,syncTargets,syncWindow};
 }
 return{palette,rgb,modelColor,cleanName,mount};
})();
