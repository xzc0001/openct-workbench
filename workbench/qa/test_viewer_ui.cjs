'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
class Element{
 constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.style={};this.attrs={};this.hidden=false;this.disabled=false;this.checked=false;this.value='';this.textContent='';this.classList={toggle:(name,value)=>{this[name]=value}}}
 append(...items){this.children.push(...items)}
 replaceChildren(...items){this.children=[...items]}
 setAttribute(key,value){this.attrs[key]=value}
 focus(){this.focused=true}
}
const ids=new Map(),get=id=>{if(!ids.has(id))ids.set(id,new Element());return ids.get(id)};
const tabs=['models','display','position'].map(key=>{const t=new Element('button');t.dataset.controls=key;return t});
const windows=['lung','soft','bone'].map(key=>{const b=new Element('button');b.dataset.window=key;return b});
get('#ww').value='1500';get('#wl').value='-600';
const context=vm.createContext({console,document:{createElement:tag=>new Element(tag),querySelector:get,querySelectorAll:key=>key==='[data-controls]'?tabs:key==='[data-window]'?windows:[]}});context.window=context;
vm.runInContext(fs.readFileSync(path.join(__dirname,'../assets/reconstruction-placeholders.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../assets/viewer-ui.js'),'utf8'),context);
const ui=context.ViewerUI,models=[{id:'lung_upper_lobe_left',name:'左上肺叶',group:'lobes',opacity:.2},{id:'lung_lower_lobe_left',name:'左下肺叶',group:'lobes',opacity:.2},{id:'airway',name:'气管与支气管（主气道连通树）',group:'airways',opacity:1},{id:'margin',name:'外扩参考',group:'margin',opacity:.2,target_id:'target_1'}];
for(const [group,token] of [['arteries','blue9'],['veins','red9'],['airways','lime9']])assert.deepEqual([...ui.modelColor({group})],[...ui.rgb(ui.palette[token])],group+' display color');
let drawings=0,invalidations=0;const meshes=new Map(models.map(m=>{m.color=ui.modelColor(m);return[m.id,{visible:m.group!=='margin',material:{}}]}));
const panel=ui.mount({D:{models},meshes,draw:()=>drawings++,invalidate:()=>invalidations++});
assert.equal(get('#layer-count').textContent,'3/3');assert.equal(models[3].check.disabled,true);
get('#layer-all').checked=false;get('#layer-all').onchange();assert.equal(models[3].check.checked,false);assert.equal(meshes.get('margin').visible,false);assert.equal(models[0].check.checked,false);
get('#layer-search').value='气管';get('#layer-search').oninput();get('#layer-all').checked=true;get('#layer-all').onchange();assert.equal(models[2].check.checked,true);assert.equal(models[0].check.checked,false,'filtered batch changed another structure');
assert.equal(get('#layer-count').textContent,'1/1');assert.equal(get('#layer-all-label').textContent,'搜索结果');
get('#layer-search').value='missing';get('#layer-search').oninput();assert.equal(get('#layer-empty').hidden,false);assert.equal(get('#layer-all').disabled,true);
get('#layer-search').value='';get('#layer-search').oninput();assert.equal(get('#layer-all').indeterminate,true);assert.equal(get('#layer-empty').hidden,true);
const group=get('#layers').children[0],groupCheck=group.children[0].children[0].children[0];groupCheck.checked=true;groupCheck.onchange();assert.equal(models[0].check.checked,true);assert.equal(groupCheck.indeterminate,false);
models[0].check.checked=false;models[0].check.onchange();assert.equal(groupCheck.indeterminate,true);
const row=group.children[2],slider=row.children[1],value=row.children[2];slider.value=37;slider.oninput();assert.equal(meshes.get(models[1].id).material.opacity,.37);assert.equal(value.textContent,'37%');
get('#target').value='target_1';panel.syncTargets();assert.equal(get('#focus').disabled,false);assert.equal(get('#margin-controls').hidden,false);assert.equal(models[3].check.disabled,true,'choosing a target bypassed review');
get('#target').value='';panel.syncTargets();assert.equal(get('#focus').disabled,true);assert.equal(get('#margin-controls').hidden,true);
get('#ww').value=400;get('#wl').value=40;panel.syncWindow();assert.equal(windows[1].attrs['aria-pressed'],'true');assert.equal(windows[0].attrs['aria-pressed'],'false');
tabs[0].onkeydown({key:'ArrowRight',preventDefault(){}});assert.equal(tabs[1].attrs['aria-selected'],'true');assert.equal(get('#controls-display').hidden,false);assert.equal(get('#controls-models').hidden,true);assert.equal(tabs[1].focused,true);
assert.equal(ui.cleanName('肺动脉（自动标签）'),'肺动脉');assert.equal(ui.cleanName('左侧（第12肋）'),'左侧（第12肋）');assert.ok(!ui.cleanName('a · b').includes('·'));assert.equal(drawings,invalidations);
const plan=[{id:'heart',group:'heart',label:'心脏',phase:'waiting'},{id:'aorta',group:'aorta',label:'主动脉',phase:'segmenting'}],future={models:[],reconstruction_plan:plan},futureMeshes=new Map();
ui.mount({D:future,meshes:futureMeshes,draw(){},invalidate(){}});
assert.equal(get('#layers').children.length,2);assert.equal(get('#layers').children[0].dataset.structure,'heart');assert.equal(get('#layers').children[1].children[1].textContent,'分割中');assert.equal(get('#layer-all').disabled,true);assert.equal(get('#layer-empty').hidden,true);
const heart={id:'heart',name:'心脏',group:'heart',color:ui.modelColor({group:'heart'}),opacity:.49};future.models.push(heart);futureMeshes.set('heart',{visible:false,material:{}});plan[0].phase='ready';
ui.mount({D:future,meshes:futureMeshes,draw(){},invalidate(){}});
assert.equal(get('#layers').children.length,2);assert.equal(get('#layers').children[0].dataset.group,'heart');assert.equal(heart.check.checked,false);assert.equal(heart.check.disabled,false);assert.equal(get('#layers').children[0].children[0].children[2].textContent,'49%');assert.equal(get('#layers').children[1].dataset.structure,'aorta');
plan[1].phase='stopped';ui.mount({D:future,meshes:futureMeshes,draw(){},invalidate(){}});assert.equal(get('#layers').children[1].children[1].textContent,'已停止');
console.log(JSON.stringify({filtered_batch:true,review_gate_preserved:true,tri_state:true,opacity:true,keyboard_tabs:true,window_preset:true,professional_names:true,placeholder_replacement:true,terminal_placeholders:true}));
