import {node} from './dom.js';
import {confirmLeave} from './shell.js';
import {dashboard} from './app.js';
import {settingsFocus} from './settings-focus.js';

/** @typedef {Record<string, any>} Wire */
/** @typedef {{value:string,baseline:string,revision:number,status:string,message:string,conflict:boolean,pending:boolean,request:number}} Draft */
const DIMENSIONS = ['uncached_input', 'cache_read', 'cache_write', 'output'];
const CONNECTION = /** @type {Record<string,string>} */ ({unconfigured:'未配置', configured_untested:'已配置未测试', connected:'已连接', error:'错误'});
const CATALOG = /** @type {Record<string,string>} */ ({unfetched:'尚未获取', fresh:'新鲜', stale:'过期', unavailable:'不可用'});
const ERRORS = /** @type {Record<string,string>} */ ({
  stale_revision:'版本冲突：草稿仍按原版本保留，请核对当前保存值。',
  external_change:'磁盘已被外部改动，写入已拒绝；当前值仍是内存 last-known-good。刷新或重新读取 .env 不能解决此冲突。',
  mutation_in_flight:'另一项设置正在保存，请稍后明确重试。', busy:'当前有运行占用，请核对阻挡本次操作的 Run。',
  endpoint_unconfigured:'缺少该端点凭据。', invalid_probe_target:'该模型已不再是已保存的指派目标。',
  model_unsupported:'模型形状不受支持。', admission_failed:'准入故障，不能据此断言没有任何记录。',
  recording_unavailable:'记录服务不可用，请核对已有运行记录。', settings_invalid:'设置无效或不可读，原文件不会被覆盖。',
  settings_schema_newer:'设置来自较新版本，禁止写回。', not_assignable:'该模型尚不符合指派条件。', pin_assigned:'已指派模型不能取消钉选。',
});
/** @param {Wire} target */
const modelKey = target => JSON.stringify([target.endpoint_id, target.model_id]);
/** @param {Wire} model @param {string} field */
function savedValue(model, field) {
  if (field === 'display') return model.display_name_override ?? '';
  if (field === 'style') return model.wire_style_source === 'user_declared' ? model.wire_style : '';
  return model.price_override?.[field] ?? '';
}
/** @param {string|null|undefined} value */
const shown = value => value === '' || value == null ? '空（无覆盖）' : value;
/** @param {Wire} body */
const errorText = body => `${ERRORS[body.cause || body.code] || '操作失败，请核对。'} (${body.code || 'unknown'}${body.cause ? ' / '+body.cause : ''})`;

/** Models owns drafts and receipts; the shell owns navigation and Stream. @param {HTMLElement} root @param {()=>string} csrf */
export function modelsPage(root, csrf) {
  root.classList.add('settings-page');
  const focus=settingsFocus(root);
  const pageNotice = node('p', '正在读取模型设置…'); pageNotice.setAttribute('role', 'status');
  const refresh = node('button', '核对当前模型设置');
  const assignments = node('section'); assignments.setAttribute('aria-label', '模型指派');
  const list = node('div'); list.setAttribute('aria-label', '模型候选');
  root.append(pageNotice, refresh, assignments, list);
  /** @type {Map<string,Draft>} */ const drafts = new Map();
  /** @type {Map<string,Wire>} */ const receipts = new Map();
  /** @type {Map<string,boolean>} */ const expanded = new Map();
  /** @type {Set<string>} */ const catalogs = new Set();
  /** @type {Wire|null} */ let current = null;
  let alive = true, generation = 0, readSequence = 0, operationSequence = 0;
  let instance = dashboard.runtime().instance || '', connected = dashboard.runtime().connected;
  let anchor = '', settingsConflict = '';
  const reads = new AbortController();
  /** @type {Wire|null} */ let restore = null;

  /** @param {Wire} model @param {string} field */
  function fieldDraft(model, field) {
    const key = JSON.stringify([model.endpoint_id, model.model_id, field]);
    let draft = drafts.get(key);
    if (!draft) {
      draft = {value:savedValue(model, field), baseline:savedValue(model, field), revision:current?.revision ?? 0, status:'', message:'', conflict:false, pending:false, request:0};
      drafts.set(key, draft);
    }
    return draft;
  }
  /** @param {Wire} model */
  function rowState(model) {
    const fields = ['display', 'style', ...DIMENSIONS].map(field=>fieldDraft(model, field));
    const labels = [];
    if (fields.some(d=>d.value!==d.baseline)) labels.push('未保存');
    if (fields.some(d=>d.pending)) labels.push('提交中');
    if (fields.some(d=>d.conflict)) labels.push('版本冲突，保留草稿');
    if (fields.some(d=>d.status==='unknown')) labels.push('保存结果未确认');
    if (fields.some(d=>d.status==='failed')) labels.push('保存失败，查看字段说明');
    const receipt = receipts.get(modelKey(model));
    if (receipt?.message) labels.push(receipt.message);
    return labels.join(' · ');
  }
  /** @param {Wire} body @param {boolean} ownWrite */
  function accept(body, ownWrite=false) {
    if (!alive || !root.isConnected) return false;
    if (current && body.revision < current.revision) return false;
    current = body;
    for (const group of body.endpoints || []) for (const model of group.models || []) {
      for (const field of ['display', 'style', ...DIMENSIONS]) {
        const draft = drafts.get(JSON.stringify([model.endpoint_id, model.model_id, field]));
        if (!draft || draft.pending || draft.conflict || draft.status==='unknown') continue;
        const saved = savedValue(model, field);
        if (draft.value===draft.baseline) {
          draft.value=saved; draft.baseline=saved; draft.revision=body.revision;
        } else if (ownWrite && saved===draft.baseline) draft.revision=body.revision;
      }
    }
    render();
    return true;
  }
  /** @param {string} [endpoint] @param {boolean} [force] */
  async function read(endpoint, force=false) {
    if (!alive) return;
    if (endpoint && catalogs.has(endpoint)) return;
    if (endpoint) catalogs.add(endpoint);
    const request = ++readSequence, epoch=generation;
    const params = new URLSearchParams();
    if (endpoint) params.set('expand',endpoint);
    if (force) params.set('refresh','1');
    render();
    try {
      const response = await fetch('/api/models'+(params.size?'?'+params:''),{signal:reads.signal});
      const body = await response.json();
      if (!alive || epoch!==generation || request!==readSequence) return;
      if (!response.ok) throw new Error(body.code || 'read_unavailable');
      if (accept(body)) pageNotice.textContent=settingsConflict || (!connected ? '连接尚未同步；设置为当前 HTTP 观察，草稿可编辑，写入暂停。' : body.status==='ok' ? `设置 revision ${body.revision}；单项保存，各项互不代存。` : `设置不可用：${body.status}；读取失败不表示无指派。`);
    } catch {
      if (alive && epoch===generation && request===readSequence) pageNotice.textContent='模型设置读取失败；已有内容仅为旧观察，草稿保留。请明确重试。';
    } finally {
      if (endpoint) catalogs.delete(endpoint);
      if (alive && epoch===generation) render();
    }
  }
  refresh.onclick=()=>void read();
  const onFocus=()=>void read();
  window.addEventListener('focus',onFocus);

  /** @param {string} op @param {Wire} model @param {Wire} fields @param {Draft|null} [draft] */
  async function mutate(op, model, fields, draft=null) {
    if (!current || !alive || !connected || !csrf() || draft?.pending || draft?.conflict || (!draft && receipts.get(modelKey(model))?.pending)) return;
    const epoch=generation, submitted=draft?.value, revision=draft?.revision ?? current.revision;
    const key=modelKey(model), attempt=++operationSequence;
    const ownsRequest=()=>alive && epoch===generation && (draft?draft.request===attempt:receipts.get(key)?.attempt===attempt);
    if (draft) {draft.request=attempt;draft.pending=true; draft.status='pending'; draft.message='正在提交；后续编辑不会改写本次请求。';}
    if (!draft) receipts.set(key,{attempt,pending:true,kind:op,message:'设置正在提交'});
    render();
    try {
      const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json','x-agent-alfred-csrf':csrf()},body:JSON.stringify({op,expected_revision:revision,endpoint_id:model.endpoint_id,model_id:model.model_id,...fields})});
      const body=await response.json();
      if (!ownsRequest()) return;
      if (!response.ok) {
        if (draft) {draft.status='failed'; draft.message=errorText(body); draft.conflict=body.code==='settings_conflict';}
        else receipts.set(key,{attempt,message:errorText(body)});
        if (body.code==='settings_conflict') {
          settingsConflict=errorText(body);pageNotice.textContent=settingsConflict;
          // A read supplies comparison only. It never retries this mutation.
          await read();
        }
        return;
      }
      if (draft) {
        const saved=body.endpoints?.flatMap((/** @type {Wire} */ g)=>g.models).find((/** @type {Wire} */ m)=>modelKey(m)===key);
        if (!saved) {draft.status='unknown'; draft.message='已收到回执，但无法核验原字段；请核对。';}
        else {
          const field=op==='price'?fields.dimension:op;
          draft.baseline=savedValue(saved,field); draft.revision=body.revision; draft.status='saved';
          if (draft.value===submitted) draft.value=draft.baseline;
          draft.message=draft.value===draft.baseline?'已确认保存':'已确认保存；还有新编辑';
        }
      } else {
        if(op==='unpin')for(const field of ['display','style',...DIMENSIONS])drafts.delete(JSON.stringify([model.endpoint_id,model.model_id,field]));
        receipts.set(key,{attempt,message:'此项设置已确认保存'});
      }
      if(![...drafts.values()].some(d=>d.conflict)){settingsConflict='';pageNotice.textContent=`设置 revision ${body.revision}；单项保存，各项互不代存。`;}
      accept(body,true);
    } catch {
      if (!ownsRequest()) return;
      if (draft) {draft.status='unknown';draft.message='保存结果未确认。先核对当前值；不会自动重送。';}
      else receipts.set(key,{attempt,message:'设置结果未确认；请核对，不会自动重送。'});
    } finally {
      if (ownsRequest()) {
        if (draft) {draft.pending=false;draft.request=0;}
        render();
      }
    }
  }

  /** @param {Wire} model @param {string} field @param {HTMLElement} summary */
  function editor(model,field,summary) {
    const draft=fieldDraft(model,field), saved=savedValue(model,field);
    const box=node('div'); box.className='setting-field';
    const label=field==='display'?'显示名':field==='style'?'线路形状':field;
    const title=node('label',label);
    const control=field==='style'?node('select'):node('input');
    control.setAttribute('aria-label',label);
    control.dataset.focusKey=JSON.stringify([model.endpoint_id,model.model_id,field]);
    if (control instanceof HTMLSelectElement) {
      for (const [value,text] of [['','内置（当前，无复位操作）'],['openai','openai'],['anthropic','anthropic']]) {
        const option=node('option',text); option.value=value; option.disabled=value===''; control.append(option);
      }
    } else if (DIMENSIONS.includes(field)) {
      control.inputMode='decimal'; control.placeholder='留空沿价格链查找';
    }
    control.value=draft.value;
    title.append(control);
    const currentValue=node('p',`当前保存值：${shown(saved)}`); currentValue.className='setting-help';
    if (field==='style') currentValue.textContent=`当前线路：${model.wire_style || '未知'} · ${model.wire_style_source || '未知'}；用户选择仍是未验证声明。`;
    if (DIMENSIONS.includes(field)) currentValue.append(document.createTextNode(' · USD / 百万 Token；用户覆盖价，空不是 0。'));
    const baseline=node('p'); baseline.className='setting-help';
    if (draft.conflict || (draft.value!==draft.baseline && draft.revision!==current?.revision)) baseline.textContent=`编辑基线：${shown(draft.baseline)} · revision ${draft.revision}；当前 revision ${current?.revision}`;
    const feedback=node('p',draft.message || (draft.value!==draft.baseline?'未保存':'')); feedback.setAttribute('role','status');
    const save=node('button',field==='display'?'保存显示名':field==='style'?'保存线路':`保存 ${field}`);
    save.dataset.focusKey=JSON.stringify([model.endpoint_id,model.model_id,'save',field]);
    const update=()=>{
      control.disabled=receipts.get(modelKey(model))?.kind==='unpin';
      save.disabled=control.disabled || !connected || current?.status!=='ok' || draft.pending || draft.conflict || draft.status==='unknown' || (field==='style' && !draft.value);
      feedback.textContent=draft.message || (draft.value!==draft.baseline?'未保存':'');
      summary.textContent=rowState(model);
    };
    control.addEventListener(field==='style'?'change':'input',()=>{
      draft.value=control.value; anchor=modelKey(model);
      if (!draft.pending && !draft.conflict && draft.status!=='unknown') draft.message='';
      update();
    });
    save.onclick=()=>{
      if (field==='display') void mutate('display',model,{display_name:draft.value || null},draft);
      else if (field==='style') void mutate('style',model,{wire_style:draft.value},draft);
      else {
        const value=draft.value.trim();
        if (value && !/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(value)) {draft.status='failed';draft.message='请输入非负十进制数，或留空移除覆盖。';update();return;}
        void mutate('price',model,{dimension:field,value:value || null},draft);
      }
    };
    box.append(title,currentValue,baseline,save,feedback);
    if (draft.conflict || draft.status==='unknown') {
      const compare=node('button','基于当前版本继续编辑');
      compare.dataset.focusKey=JSON.stringify([model.endpoint_id,model.model_id,'adopt',field]);
      compare.disabled=!connected || current?.status!=='ok' || draft.message.includes('external_change');
      compare.onclick=()=>{draft.baseline=saved;draft.revision=current?.revision ?? draft.revision;draft.conflict=false;draft.status='';draft.message='已采用当前版本；原请求不再重送，保存需再次明确点击。';if(![...drafts.values()].some(d=>d.conflict)){settingsConflict='';pageNotice.textContent=`设置 revision ${current?.revision}；已核对，保存仍需明确点击。`;}render();};
      box.append(compare);
    }
    update();
    return box;
  }

  /** @param {Wire} model */
  async function inferenceProbe(model) {
    const key=modelKey(model);
    if (receipts.get(key)?.pending || !connected || !csrf()) return;
    const epoch=generation, attempt=++operationSequence;
    receipts.set(key,{attempt,pending:true,message:'正在提交模型探针；按已保存设置，后续编辑不参与。'});render();
    try {
      const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json','x-agent-alfred-csrf':csrf()},body:JSON.stringify({message:'inference probe',purpose:'inference_probe',endpoint_id:model.endpoint_id,model_id:model.model_id})});
      const body=await response.json();
      if (!alive || epoch!==generation || receipts.get(key)?.attempt!==attempt) return;
      if (response.status===202 && typeof body.run_id==='string' && body.run_id) receipts.set(key,{attempt,message:'探针已受理；不代表完成、连接成功或记录已保存。',runId:body.run_id});
      else if (response.ok) receipts.set(key,{attempt,message:'探针受理结果未确认；请到运行记录核对，不会自动重送。'});
      else receipts.set(key,{attempt,message:errorText(body),blockingRun:typeof body.run_id==='string'?body.run_id:null});
    } catch {
      if (alive && epoch===generation) receipts.set(key,{attempt,message:'探针受理结果未确认；请到运行记录核对，不会自动重送。'});
    } finally {if(alive && epoch===generation)render();}
  }

  /** @param {Wire} model @param {string} id @param {string} label */
  function runLink(model,id,label) {
    const link=node('a',label);link.dataset.focusKey=JSON.stringify([model.endpoint_id,model.model_id,'run',id]);link.href='/runs/'+encodeURIComponent(id)+'?filter=system';
    link.onclick=event=>{if(event.button!==0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)return;event.preventDefault();void dashboard.navigate(link.href,{intent:'source',source:{returnSource:{route:'/models',kind:'model',anchor:modelKey(model),section:'model',process_instance_id:instance}}});};
    return link;
  }

  function render() {
    if (!alive || !current || !root.isConnected) return;
    const focusRequest=focus.capture();
    assignments.replaceChildren(node('h2','模型指派'));
    const describe=(/** @type {Wire|null|undefined} */ value)=>value?`${value.endpoint_id} / ${value.model_id}`:'未知';
    if (current.status!=='ok') assignments.append(node('p',`设置不可用：${current.status}；不能确认当前指派。`));
    else assignments.append(node('p','主模型：'+describe(current.assignments?.primary)),node('p','辅助小模型：'+(current.assignments?.retrieval_gate?describe(current.assignments.retrieval_gate):'跟随主模型')),node('p','辅助小模型用于记忆检索门、Skill 自动选择与消息分类。'));
    list.replaceChildren();
    for (const group of current.endpoints || []) {
      const section=node('section');section.className='settings-group';section.dataset.endpoint=group.endpoint_id;
      const connection=node('p',`连接：${CONNECTION[group.observation?.state] || group.observation?.state || '未知'} ${group.observation?.reason || ''} ${group.observation?.checked_at || ''} ${group.observation?.checked_via || ''}`);connection.dataset.dimension='connection';
      const catalog=node('p',`目录：${CATALOG[group.catalog.health] || group.catalog.health}；上次成功 ${group.catalog.last_success_at || '尚无'} ${group.catalog.last_error || ''} ${group.catalog.retry_at?'重试 '+group.catalog.retry_at:''}`);catalog.dataset.dimension='catalog';
      const open=node('button','展开此端点');open.dataset.focusKey=JSON.stringify([group.endpoint_id,'expand']);open.disabled=!connected;open.onclick=()=>void read(group.endpoint_id);
      const reload=node('button',catalogs.has(group.endpoint_id)?'目录读取中…':'刷新目录');reload.dataset.focusKey=JSON.stringify([group.endpoint_id,'refresh']);reload.disabled=catalogs.has(group.endpoint_id)||!connected;reload.onclick=()=>void read(group.endpoint_id,true);
      section.append(node('h2',group.endpoint_id),connection,catalog,open,reload);
      if (!group.models.length) section.append(node('p','暂无候选；目录尚未获取、失败或为空均不代表当前无指派。'));
      for (const model of group.models) {
        const key=modelKey(model), row=node('article');row.className='model-row';row.dataset.model=`${model.endpoint_id}:${model.model_id}`;row.dataset.modelKey=key;
        const support=node('p',`支持状态：${model.support_label} ${model.reason || ''}`);support.dataset.dimension='support';
        const primary=modelKey(current.assignments?.primary || {})===key, gate=modelKey(current.assignments?.retrieval_gate || {})===key;
        row.append(node('h3',model.display_name || model.model_id),node('p',model.model_id),support,node('p',`${model.pinned?'已钉选':'未钉选'}${primary?' · 当前主模型':''}${gate?' · 当前辅助小模型':''}`));
        const summary=node('p',rowState(model));summary.className='model-row-state';row.append(summary);
        const details=node('details'), toggle=node('summary','模型详情');toggle.dataset.focusKey=key+'detail';details.append(toggle);details.open=expanded.get(key) ?? false;
        details.addEventListener('toggle',()=>{expanded.set(key,details.open);if(!details.open && details.contains(document.activeElement))toggle.focus({preventScroll:true});});
        details.append(node('p',`endpoint_id：${model.endpoint_id}`),node('p',`model_id：${model.model_id}`),node('p',`候选来源：${(model.sources || []).join(' / ') || '未知'}；支持依据：${model.support_basis || '未知'}`),node('p',`线路：${model.wire_style || '未知'}；线路依据：${model.wire_style_source || '未知'}`));
        const actions=node('div');actions.className='settings-actions';
        if(model.pinned) {
          const unpin=node('button','取消钉选');unpin.dataset.focusKey=JSON.stringify([model.endpoint_id,model.model_id,'unpin']);unpin.disabled=primary||gate||!connected||Boolean(receipts.get(key)?.pending)||['display','style',...DIMENSIONS].some(f=>fieldDraft(model,f).pending);
          unpin.onclick=async()=>{const changed=['display','style',...DIMENSIONS].some(f=>{const d=fieldDraft(model,f);return d.value!==d.baseline;});if(changed && !await confirmLeave({dirty:true,summary:'取消钉选将放弃此模型的未保存编辑。'}))return;void mutate('unpin',model,{});};
          actions.append(unpin);
          if(primary||gate)actions.append(node('p','已指派，不能取消钉选。'));
        } else {const pin=node('button','钉选');pin.dataset.focusKey=JSON.stringify([model.endpoint_id,model.model_id,'pin']);pin.disabled=!connected;pin.onclick=()=>void mutate('pin',model,{});actions.append(pin);}
        for(const [slot,label] of [['primary','指派为主模型'],['retrieval_gate','指派为检索门']]) {
          const assign=node('button',label);assign.dataset.focusKey=JSON.stringify([model.endpoint_id,model.model_id,'assign',slot]);assign.disabled=!model.assignable||!connected;assign.onclick=()=>void mutate('assign',model,{slot});actions.append(assign);
        }
        if(!model.assignable)actions.append(node('p',`不可指派：${!model.pinned?'请先钉选':model.disable_code || '支持依据不足'}（${model.disable_dimension || 'pin'}）。连接观测不参与指派。`));
        details.append(actions);
        if(model.pinned)for(const field of ['display','style',...DIMENSIONS])details.append(editor(model,field,summary));
        const probe=node('section');probe.className='model-probe';probe.setAttribute('aria-label','真实调用测试');
        probe.append(node('h4','真实调用测试'),node('p',`${model.endpoint_id} / ${model.model_id} · 仅按已保存设置测试；可能产生费用，未保存编辑不会纳入。`));
        const button=node('button','测试真实调用（可能计费）');button.dataset.focusKey=JSON.stringify([model.endpoint_id,model.model_id,'probe']);button.disabled=!model.probe_enabled||!connected||Boolean(receipts.get(key)?.pending);button.onclick=()=>void inferenceProbe(model);probe.append(button);
        if(!model.probe_enabled)probe.append(node('p',!model.probe?'需先钉选并指派此模型。':'缺少该端点凭据。'));
        const receipt=receipts.get(key);if(receipt?.message)probe.append(node('p',receipt.message));
        if(receipt?.runId)probe.append(runLink(model,receipt.runId,`查看探针 Run ${receipt.runId}`));
        if(receipt?.blockingRun)probe.append(runLink(model,receipt.blockingRun,`查看阻挡本次操作的 Run ${receipt.blockingRun}`));
        if(receipt && !receipt.runId){const all=node('a','核对运行记录');all.dataset.focusKey=JSON.stringify([model.endpoint_id,model.model_id,'runs']);all.href='/runs?filter=system';probe.append(all);}
        details.append(probe);row.append(details);section.append(row);
      }
      list.append(section);
    }
    focus.restore(focusRequest);
    if(restore) {
      const target=[...list.querySelectorAll('[data-model-key]')].find(e=>/** @type {HTMLElement} */(e).dataset.modelKey===restore?.anchor);
      if(target){target.scrollIntoView({block:'nearest'});restore=null;}
      else {pageNotice.textContent='来源模型已不可用；当前模型列表可供核对。';restore=null;}
    }
  }
  const stop=dashboard.subscribeState(state=>{
    const changed=Boolean(state.instance && state.instance!==instance);
    const resumed=state.connected && !connected;
    const connectivityChanged=Boolean(state.connected)!==connected;
    connected=Boolean(state.connected);
    if(changed){instance=state.instance;generation++;readSequence++;current=null;for(const draft of drafts.values()){draft.pending=false;draft.conflict=true;draft.status='unknown';draft.message='运行实例已变更；保留原草稿，先核对当前版本。';}pageNotice.textContent='实例已变更，正在核对当前设置。';}
    if(changed || resumed)void read();
    if(!connected && connectivityChanged){
      generation++;readSequence++;
      for(const draft of drafts.values())if(draft.pending){draft.pending=false;draft.status='unknown';draft.message='连接中断，保存结果未确认；不会自动重送。';}
      for(const receipt of receipts.values())if(receipt.pending){receipt.pending=false;receipt.message='连接中断，原操作结果未确认；请核对运行或设置，不会自动重送。';}
      pageNotice.textContent='连接中断；设置仅为旧观察，草稿保留，操作暂停。';
    }
    if(changed || connectivityChanged)render();
  });
  void read();
  return {
    getLeaveState:()=>({dirty:[...drafts.values()].some(d=>d.value!==d.baseline),pending:[...drafts.values()].some(d=>d.pending)||[...receipts.values()].some(r=>r.pending),summary:'模型设置有未保存输入；已提交设置或探针不会因离开而撤销。'}),
    captureSource:()=>({route:'/models',kind:'model',anchor,process_instance_id:instance}),
    restoreSource:(/** @type {Wire} */ source)=>{if(source?.anchor){anchor=source.anchor;expanded.set(anchor,true);restore=source;render();}},
    close(){alive=false;generation++;reads.abort();stop();focus.close();window.removeEventListener('focus',onFocus);},
  };
}
