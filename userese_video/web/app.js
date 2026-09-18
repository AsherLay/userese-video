'use strict';
const $ = id => document.getElementById(id);
let project, catalog, cues, state, current = 0, activeTake, dirty = false, saving = false, manifests = [], activeBuild;
let captionEdits = {}, jobBusy = false;
const labels = {pending:'◌ 待确认', keep:'✓ 使用', skip:'− 不放', optimize:'↻ 待优化'};
const node = (tag, value, className) => {const el = document.createElement(tag); if(value !== undefined) el.textContent = value; if(className) el.className = className; return el;};
function notify(message) { $('notice').textContent = message; $('notice').hidden = false; }
function clearNotice() { $('notice').hidden = true; }
async function api(path, payload) {
  const options = payload === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)};
  const response = await fetch(path, options);
  const data = await response.json();
  if(!response.ok) { if(response.status === 401) { $('login').hidden = false; $('workspace').hidden = true; } throw new Error(data.error || '请求失败'); }
  return data;
}
function action(fn) {return async event => {try {clearNotice(); await fn(event);} catch(error) {notify(error.message);}};}
function markDirty() {dirty = true; $('save-receipt').textContent = '有未保存修改'; $('save-receipt').classList.add('dirty');}
function canLeave() {if(saving) return false; if(dirty) {notify('先保存备注与字幕，再切换段落或候选。也可以导出本页决定备份。'); return false;} return true;}
function takeCues(take) {return cues.filter(cue => take.parts.some(part => cue.source === part.source && cue.end > part.start + .02 && cue.start < part.end - .02));}
function family() {return catalog[current];}
function mergeProposals() {
  for(const item of catalog) {
    for(const take of state.proposals?.[item.id] || []) {
      if(!item.takes.some(existing=>existing.id===take.id)) item.takes.push(take);
    }
  }
}
function renderNav() {
  $('segments').replaceChildren(); $('segment-select').replaceChildren();
  catalog.forEach((item, index) => {
    const choice = state.choices[item.id];
    const status = labels[choice.status] + (choice.note ? ' · 有备注' : '');
    const button = node('button', undefined, index === current ? 'active' : '');
    button.setAttribute('aria-current', index === current ? 'step' : 'false');
    button.append(node('span', String(index + 1).padStart(2,'0'), 'index'));
    const copy = node('span', undefined, 'segment-copy'); copy.append(node('span',item.title,'segment-name'),node('small',status)); button.append(copy);
    button.addEventListener('click', () => {if(canLeave()) {current = index; render();}}); $('segments').append(button);
    const option = node('option', `${index+1}. ${item.title} · ${labels[choice.status]}`); option.value = index; $('segment-select').append(option);
  });
  $('segment-select').value = current;
  $('progress-count').textContent = `${Object.values(state.choices).filter(choice => ['keep','skip'].includes(choice.status)).length} / ${catalog.length}`;
  $('segment-count').textContent = `${catalog.length} 段`;
}
function renderCaptions() {
  $('captions').replaceChildren(); captionEdits = {};
  for(const cue of takeCues(activeTake)) {
    const row = node('div',undefined,'caption-row');
    row.append(node('p',`原始 · ${cue.start.toFixed(2)}–${cue.end.toFixed(2)}s · ${cue.text}`));
    const label = node('label','成片字幕'); label.htmlFor = `caption-${cue.id}`;
    const input = node('input'); input.id = label.htmlFor; input.maxLength = 200; input.value = state.captions[cue.id] ?? cue.text;
    input.addEventListener('input', () => {captionEdits[cue.id] = input.value; markDirty();}); row.append(label,input); $('captions').append(row);
  }
}
function renderTakes() {
  $('takes').replaceChildren();
  for(const take of family().takes) {
    const button = node('button', undefined, 'take' + (take.id === activeTake.id ? ' active' : '')); button.dataset.take = take.id;
    button.setAttribute('aria-pressed', String(take.id === activeTake.id));
    const label = node('span', undefined,'take-label'); label.append(node('span',take.label));
    const markers = [];
    if(take.id === family().suggested) markers.push('建议');
    if(take.id === state.choices[family().id].take && state.choices[family().id].status === 'keep') markers.push('已选');
    if(take.parts.length > 1) markers.push('拼接候选');
    label.append(node('small',markers.join(' · ')));
    button.append(label,node('span',takeCues(take).map(cue=>cue.text).join(' '),'take-text'));
    button.addEventListener('click', () => {if(canLeave()) {activeTake = take; renderTakes(); renderCaptions(); setOriginal(false);}});
    $('takes').append(button);
  }
}
function setOriginal(play) {
  const part = activeTake.parts[0];
  $('player').src = `/media/source/${encodeURIComponent(part.source)}#t=${part.start},${part.end}`;
  $('playing-label').textContent = activeTake.parts.length > 1 ? '第一原片区间 · 单独听可试听完整拼接' : '原片区间预览';
  if(play) $('player').play().catch(()=>{});
}
function render() {
  renderNav(); const item = family(); const choice = state.choices[item.id];
  activeTake = item.takes.find(take => take.id === choice.take) || item.takes[0];
  $('segment-number').textContent = `SEGMENT ${String(current+1).padStart(2,'0')} / ${catalog.length}`;
  $('segment-title').textContent = item.title; $('saved-status').textContent = labels[choice.status];
  $('decision-summary').textContent = choice.status === 'keep' ? `已选：${activeTake.label}` : choice.status === 'skip' ? '这一段暂不进入成片。' : choice.status === 'optimize' ? '修改意见已留存，处理后再确认。' : '当前选择仍是建议，听过之后再决定。';
  $('note').value = choice.note; $('save-receipt').textContent = `已保存 · 修改版本 ${state.revision}`; $('save-receipt').classList.remove('dirty');
  renderTakes(); renderCaptions(); setOriginal(false);
}
function proposedState(status) {
  const proposed = structuredClone(state); const choice = proposed.choices[family().id];
  choice.note = $('note').value;
  if(status) {choice.status = status; choice.take = status === 'skip' ? null : activeTake.id;}
  for(const [id,value] of Object.entries(captionEdits)) {
    const original = cues.find(cue => cue.id === id).text;
    if(value === original) delete proposed.captions[id]; else proposed.captions[id] = value;
  }
  return proposed;
}
async function save(status) {
  if(saving) return;
  const proposed = proposedState(status); saving = true;
  const controls = [...document.querySelectorAll('#workspace button,#workspace input,#workspace textarea,#workspace select')];
  const previouslyDisabled = controls.map(control => control.disabled); controls.forEach(control => {control.disabled = true;});
  $('save-receipt').textContent = '正在保存…';
  try {
    state = await api('/api/state',{state:proposed,expected_revision:state.revision}); mergeProposals(); dirty = false;
    // Keep the candidate being browsed after a note-only save.
    const browsed = activeTake.id; render(); activeTake = family().takes.find(take=>take.id===browsed); renderTakes(); renderCaptions();
    $('save-receipt').textContent = `已保存到本机 · 修改版本 ${state.revision}`;
  } catch(error) {markDirty(); $('save-receipt').textContent = '保存失败 · 本页修改仍在，可先导出'; throw error;}
  finally {saving = false; controls.forEach((control,i) => {control.disabled = previouslyDisabled[i];});}
}
async function job(path, payload, onComplete) {
  if(jobBusy) throw new Error('已有任务正在处理，请等待完成');
  const started = await api(path,payload); jobBusy = true;
  ['listen','context','build'].forEach(id=>{$(id).disabled=true;});
  $('job-status').textContent = path.includes('build') ? '正在生成并检查新版本，原有成片保持不变…' : '正在准备连续试听…';
  try {
    for(;;) {
      await new Promise(resolve => setTimeout(resolve,700));
      const result = await api('/api/job');
      if(result.id !== started.id) throw new Error('任务记录已更新，请刷新查看最新成片');
      if(result.status === 'failed') throw new Error(result.error);
      if(result.status === 'complete') {await onComplete(result.result); break;}
    }
  } catch(error) {$('job-status').textContent = '处理未完成：' + error.message; throw error;}
  finally {jobBusy=false; ['listen','context','build'].forEach(id=>{$(id).disabled=false;});}
}
async function listen(context) {
  if(!canLeave()) return;
  const label = context ? '已保存邻段 + 当前候选 + 已保存邻段' : activeTake.label;
  await job('/api/preview',{take:activeTake.id,context}, async result => {
    $('player').src = '/media/preview/' + result.key; $('playing-label').textContent = label;
    $('job-status').textContent = '试听已准备好。'; await $('player').play().catch(()=>{});
  });
}
function showBuild(id) {
  activeBuild = manifests.find(item=>item.id===id); if(!activeBuild) return;
  $('history').value = id; $('draft-player').src = `/build/${id}/video.mp4`;
  $('build-info').textContent = `${activeBuild.draft?'建议版':'已确认版'} · ${activeBuild.duration.toFixed(2)} 秒 · ${activeBuild.width} × ${activeBuild.height} · 修改版本 ${activeBuild.revision}`;
  $('downloads').replaceChildren();
  for(const [name,label] of [['delivery.zip','下载完整交付包'],['video.mp4','下载视频 MP4'],['captions.srt','下载字幕 SRT'],['cover.jpg','下载首帧封面 JPEG']]) {
    const anchor=node('a',label); anchor.href=`/build/${id}/${name}?download=1`; anchor.download=name; $('downloads').append(anchor);
  }
}
async function loadHistory() {
  manifests = await api('/api/history'); $('history').replaceChildren();
  for(const manifest of manifests) {const option=node('option',`${manifest.id} · r${manifest.revision}${manifest.draft?' · 建议版':''}`);option.value=manifest.id;$('history').append(option);}
  $('delivery-content').hidden = !manifests.length; $('empty-history').hidden = !!manifests.length;
  if(manifests.length) showBuild(manifests[0].id);
}
async function boot() {
  const data = await api('/api/project'); ({project,catalog,cues}=data); state = data.state;
  $('login').hidden=true; $('workspace').hidden=false; $('project-title').textContent=project.title;
  $('source-cues').replaceChildren();
  for(const cue of cues) {
    const row=node('div',undefined,'audit-row');const button=node('button',`${cue.start.toFixed(1)}–${cue.end.toFixed(1)}s`);
    button.addEventListener('click',()=>{$('player').src=`/media/source/${cue.source}#t=${cue.start}`;$('playing-label').textContent='完整原片 · ' + cue.source;$('player').play().catch(()=>{});$('player').scrollIntoView({block:'center'});});
    row.append(button,node('span',cue.text));$('source-cues').append(row);
  }
  render(); await loadHistory();
  const pending = await api('/api/job');
  if(pending.status==='running') $('job-status').textContent='已有任务仍在后台处理；完成后刷新查看结果。';
}
$('segment-select').addEventListener('change',()=>{if(canLeave()){current=Number($('segment-select').value);render();}else $('segment-select').value=current;});
$('note').addEventListener('input',markDirty);
$('keep').addEventListener('click',action(()=>save('keep')));
$('skip').addEventListener('click',action(()=>save('skip')));
$('optimize').addEventListener('click',action(()=>save('optimize')));
$('save').addEventListener('click',action(()=>save()));
$('listen').addEventListener('click',action(()=>listen(false)));
$('context').addEventListener('click',action(()=>listen(true)));
$('original').addEventListener('click',()=>{const part=activeTake.parts[0];$('player').src=`/media/source/${part.source}#t=${part.start}`;$('playing-label').textContent='完整原片';$('player').play().catch(()=>{});});
$('build').addEventListener('click',action(async()=>{
  if(!canLeave()) return;
  await job('/api/build',{draft:$('draft').checked,expected_revision:state.revision},async result=>{await loadHistory();showBuild(result.id);$('job-status').textContent='新版本已完成，视频解码与音画时长检查通过。请试听首尾与衔接，再决定是否发布。';});
}));
$('export').addEventListener('click',()=>{
  const blob=new Blob([JSON.stringify(proposedState(),null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);
  const link=node('a');link.href=url;link.download='decisions.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
$('import').addEventListener('change',action(async()=>{
  const file=$('import').files[0]; if(!file) return;
  try {if(!canLeave()) return;if(file.size>2*1024*1024) throw new Error('快照超过 2 MiB');
    const imported=JSON.parse(await file.text());state=await api('/api/state',{state:imported,expected_revision:state.revision});mergeProposals();dirty=false;render();
    $('save-receipt').textContent=`已导入并保存 · 修改版本 ${state.revision}`;
  } finally {$('import').value='';}
}));
$('history').addEventListener('change',()=>showBuild($('history').value));
$('jump').addEventListener('click',()=>{if(!canLeave()||!activeBuild)return;const time=$('draft-player').currentTime;const block=activeBuild.blocks.find(item=>time>=item.output_start&&time<item.output_end)||activeBuild.blocks.at(-1);current=catalog.findIndex(item=>item.id===block.family);render();$('segment-title').scrollIntoView({block:'start'});});
$('login-form').addEventListener('submit',action(async event=>{event.preventDefault();await api('/api/session',{token:$('token').value});$('token').value='';await boot();}));
window.addEventListener('beforeunload',event=>{if(dirty||saving){event.preventDefault();event.returnValue='';}});
(async()=>{try {const params=new URLSearchParams(location.hash.slice(1));const token=params.get('token');if(token){window.history.replaceState(null,'',location.pathname);await api('/api/session',{token});}await boot();}catch(error){notify(error.message);}})();
