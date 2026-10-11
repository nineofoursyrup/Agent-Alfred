import {node} from './dom.js';
import {dashboard} from './app.js';

/** @typedef {Record<string, any>} Wire */
/** @param {string} label @param {string} href */
function link(label, href) { const a=node('a',label); a.href=href; return a; }
/** @param {string} title */
function section(title) {const box=node('section');box.setAttribute('aria-label',title);const heading=node('h2',title);heading.className='sy-panel-eyebrow';box.append(heading);return box;}
/** @param {HTMLElement} box @param {string} description */
function asPanel(box,description) {
  box.classList.add('sy-panel');
  const header=node('header'),titles=node('div'),note=node('p',description);
  header.className='sy-panel-header';note.className='sy-panel-description';
  const heading=box.querySelector('h2');if(heading)titles.append(heading);
  titles.append(note);header.append(titles);box.append(header);
  return header;
}
/** @param {string} title */
function metric(title) {
  const card=node('article'),label=node('h3',title);
  card.className='sy-metric-card overview-metric';label.className='sy-metric-label';card.append(label);
  return card;
}
/** @param {string} value @param {string} caption @param {boolean} [success] */
function metricValue(value,caption='',success=false) {
  const text=node('p',value),note=node('span',caption);
  text.className='sy-metric-value'+(success?' sy-tone-success':'');
  note.className='overview-metric-caption sy-metric-label';if(caption)text.append(note);
  return text;
}
/** @param {string} value */
function time(value) {return Number.isFinite(Date.parse(value)) ? value : '时间未知';}
/** Decimal text is never accumulated or rounded through binary floating point. @param {string} value */
function money(value) {
  if(!/^\d+(\.\d+)?$/.test(value))return '金额未知';
  const [whole,fraction='']=value.split('.');
  if(/^0+$/.test(whole)&&/^0{4}/.test(fraction)&&/[1-9]/.test(fraction))return '<0.0001';
  return value;
}
/** @param {Wire} reasons */
function coverageReasons(reasons) {
  const labels=/** @type {Record<string,string>} */({historic_tool_metering_unrecorded:'历史工具计量未记录',telemetry_unreadable_or_missing:'模型收尾账目缺失／不可读',attempt_count_unconfirmed:'调用数量未确认',recording_pending:'记录尚未落定',recording_failed:'记录失败',damaged_attempt:'Attempt 账目损坏',time_unrecorded:'时间未记录'});
  return Object.entries(reasons).map(([key,count])=>`${labels[key]||key} ${count} 条 Run`).join('；');
}

/** A source owns its data, read identity and observation deadline, never a page-wide clock. */
class Source {
  /** @param {HTMLElement} box @param {string} label @param {(value:Wire|null)=>void} render */
  constructor(box,label,render) {
    this.render=render;this.label=label;
    this.status=node('p','等待来源核验');this.status.setAttribute('role','status');this.status.className='overview-source-status sy-row-meta';
    this.refresh=node('button',`刷新${label}`);box.append(this.refresh,this.status);
    /** @type {Wire|null} */ this.value=null;
    /** @type {AbortController|null} */ this.request=null;
    this.generation=0;this.loading=false;this.failure='';this.stale='';this.offline=false;this.deadline=0;this.timer=0;
  }
  retire() {this.generation++;this.request?.abort();this.request=null;this.loading=false;}
  clear(reason='') {this.retire();clearTimeout(this.timer);this.value=null;this.deadline=0;this.stale=reason;this.failure='';this.render(null);this.present();}
  present() {
    const parts=[];
    if(this.loading)parts.push(this.value?'正在刷新，保留原快照':'正在读取');
    if(this.failure)parts.push(`读取失败：${this.failure}`);
    if(this.offline)parts.push(this.value?'断连 · 离线副本':'断连，等待连接核验');
    if(this.value && Date.now()>=this.deadline)parts.push('已过期，请刷新');
    if(this.stale)parts.push(this.stale);
    if(this.value)parts.push(`来源观察：${time(this.value.computed_at||this.value.observed_at)}`);
    this.status.textContent=parts.join(' · ') || '等待来源核验';
    this.refresh.disabled=this.loading||this.offline;
  }
  /** @param {Wire} value */
  accept(value) {
    this.value=value;this.failure='';this.stale='';this.loading=false;
    const observed=Date.parse(value.computed_at||value.observed_at),expires=Date.parse(value.expires_at);
    this.deadline=Number.isFinite(observed)&&Number.isFinite(expires)?Math.min(observed+900000,expires):0;
    clearTimeout(this.timer);
    this.timer=window.setTimeout(()=>this.present(),Math.max(0,this.deadline-Date.now()));
    this.render(value);this.present();
  }
  dispose(){this.retire();clearTimeout(this.timer);}
}

/** @param {HTMLElement} root @param {{url:URL,replaceSource:(url:string)=>void}} options */
export function overviewPage(root,options) {
  root.classList.add('overview');
  const query=options.url.searchParams;
  let range=query.get('range')??'7d';
  let timezone=query.get('timezone')??Intl.DateTimeFormat().resolvedOptions().timeZone;
  let disposed=false,started=false;
  /** @type {Wire} */ let state={};
  const refreshAll=node('button','刷新全部来源');root.append(refreshAll);
  const returnNotice=node('p');returnNotice.setAttribute('role','status');root.append(returnNotice);
  let anchor='',focusHref='',sectionKey='';
  /** @type {Wire|null} */ let pendingRestore=null;
  const cancelRestore=()=>{pendingRestore=null;};
  document.addEventListener('pointerdown',cancelRestore);document.addEventListener('keydown',cancelRestore);
  function captureSource(){return {kind:'overview',route:'/overview?'+new URLSearchParams({range,timezone:timezone||''}),range,timezone,anchor,focusHref,section:sectionKey,process_instance_id:state.instance};}
  root.addEventListener('click',event=>{
    if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
    const target=event.target instanceof Element?event.target.closest('a'):null;
    if(!target||target.origin!==location.origin||target.target)return;
    anchor=target.closest('[data-run-id]')?.getAttribute('data-run-id')??'';
    sectionKey=target.closest('section')?.getAttribute('aria-label')??'';focusHref=target.getAttribute('href')??'';
    event.preventDefault();void dashboard.navigate(target.href,{intent:'source',source:{returnSource:captureSource()}});
  });
  function restoreFocus(){
    const origin=pendingRestore;if(!origin)return;
    const source=origin.section==='期间指标'?periodSource:origin.section==='当前记忆条目数'?memorySource:origin.section==='全历史最近运行'?recentSource:null;
    if(source&&!source.value)return;
    pendingRestore=null;
    const region=origin.section?[...root.querySelectorAll('section')].find(item=>item.getAttribute('aria-label')===origin.section):root;
    const target=[...(region?.querySelectorAll('a')||[])].find(item=>item.getAttribute('href')===origin.focusHref&&(item.closest('[data-run-id]')?.getAttribute('data-run-id')??'')===(origin.anchor??''));
    if(target){target.focus({preventScroll:true});returnNotice.textContent=source?'已重新读取来源。':'已恢复来源入口。';}
    else {returnNotice.textContent='原来源目标已不在本次结果中；已重新读取，未替换为其他目标。';returnNotice.tabIndex=-1;returnNotice.focus({preventScroll:true});}
  }
  const metrics=node('div');metrics.className='overview-metrics';root.append(metrics);
  const period=section('期间指标');period.classList.add('overview-period');metrics.append(period);
  const periodBody=node('div');periodBody.className='overview-period-cards';period.append(periodBody);
  const periodControls=node('div');periodControls.className='overview-controls';period.append(periodControls);
  const periodEvidence=node('div');periodEvidence.className='overview-source-detail';
  const rangeLabel=node('label','总览期间');const select=node('select');select.setAttribute('aria-label','总览期间');
  for(const [value,label] of [['today','今天'],['7d','最近七个自然日'],['30d','最近三十个自然日']]){const option=node('option',label);option.value=value;select.append(option);}
  if(!['today','7d','30d'].includes(range)){const invalid=node('option','期间不可用，请重新选择');invalid.value=range;select.append(invalid);}
  select.value=range;rangeLabel.append(select);periodControls.append(rangeLabel);
  const zone=node('label','IANA 时区');const zoneInput=node('input');zoneInput.setAttribute('aria-label','IANA 时区');zoneInput.value=timezone||'';
  const applyZone=node('button','应用时区');zone.append(zoneInput);periodControls.append(zone,applyZone);
  const periodSource=new Source(period,'期间指标',renderPeriod);periodControls.append(periodSource.refresh);period.append(periodEvidence);
  const memory=section('当前记忆条目数');memory.classList.add('overview-memory');metrics.append(memory);
  const memoryBody=node('div');memoryBody.className='overview-memory-cards';memory.append(memoryBody);
  const memoryControls=node('div');memoryControls.className='overview-controls';memory.append(memoryControls);
  const memoryEvidence=node('div');memoryEvidence.className='overview-source-detail';
  const memorySource=new Source(memory,'记忆计数',renderMemory);memoryControls.append(memorySource.refresh);
  memory.append(memoryEvidence,node('p','当前存量；不随期间变化。不等同于可自动输入的条目，删除计数不证明遗忘清理完成。'),link('查看当前记忆（重新读取）','/memory'));
  renderArchitecture(root);
  const recent=section('全历史最近运行');recent.classList.add('overview-recent');root.append(recent);
  const recentHeader=asPanel(recent,'最多五条已结束 Run，与期间指标范围不同；当前槽来自共享宿主状态。');
  const recentBody=node('div'),current=node('div'),notice=node('p');
  recentBody.className='overview-history sy-row-list';current.className='overview-current sy-row-list';current.setAttribute('aria-label','当前运行槽');
  notice.setAttribute('role','status');
  const recentSource=new Source(recent,'最近运行',renderRecent);
  recentHeader.append(recentSource.refresh);
  const recentStatus=node('div');recentStatus.className='overview-recent-status sy-panel-body';recentStatus.append(recentSource.status,notice);
  recent.append(recentStatus,current,recentBody);
  const recentFooter=node('div');recentFooter.className='sy-panel-body';recentFooter.append(link('查看全部运行','/runs?filter=all'));recent.append(recentFooter);
  /** @type {Wire|null} */ let slot=null;
  let slotSignature='';

  /** @param {Wire|null} value */
  function renderPeriod(value) {
    periodBody.replaceChildren();periodEvidence.replaceChildren();
    const runs=metric('运行记录数'),cost=metric('模型费用');periodBody.append(runs,cost);
    if(!value){runs.append(metricValue('—'));cost.append(metricValue('—'));return;}
    const s=value.summary,u=value.unresolved_membership_count;
    runs.append(metricValue(u?'—':String(s.run_count),u?` 已定位 ${s.run_count} 条；另 ${u} 条期间无法判定`:s.run_count?' 条 Run 记录':' 暂无运行记录'));
    const exactPositive=/[1-9]/.test(s.exact_usd),estimatedPositive=/[1-9]/.test(s.estimated_usd);
    const proven=value.model_coverage.complete&&u===0&&s.unknown_cost_attempts===0;
    const empty=s.run_count===0&&u===0;
    if(empty)cost.append(metricValue('—',' 暂无运行记录'));
    else if(proven&&s.attempt_count===0)cost.append(metricValue('—',' 该范围无模型消耗'));
    else if(proven&&!exactPositive&&!estimatedPositive)cost.append(metricValue('0',' 该范围零费用',true));
    else if(!s.exact_attempts&&!s.estimated_attempts)cost.append(metricValue('—',' 费用未知；尚无可确认的完整范围金额'));
    else {
      const amounts=node('div');amounts.className='overview-cost-values';
      for(const [label,amount,count] of [['精确',s.exact_usd,s.exact_attempts],['估算',s.estimated_usd,s.estimated_attempts]]) {
        if(!count)continue;
        const text=metricValue(money(amount),`${label} USD `,true);
        if(text.lastChild)text.prepend(text.lastChild);amounts.append(text);
      }
      cost.append(amounts);
    }
    const costFacts=[`已记录 Attempt ${s.attempt_count} 次 · 费用未知 ${s.unknown_cost_attempts} 次`,value.model_coverage.complete?'模型账目完整':`模型账目覆盖不足 ${value.model_coverage.incomplete_runs} 条 Run：${coverageReasons(value.model_coverage.reasons)}`];
    if(u)costFacts.push(`另 ${u} 条期间无法判定`);
    for(const fact of costFacts){const text=node('p',fact);text.className='sy-metric-label';cost.append(text);}
    if(s.exact_attempts)periodEvidence.append(node('p','精确金额来自端点报告，未经外部账单核对。'));
    if(s.estimated_attempts)periodEvidence.append(node('p',`估算价格来源：${Object.keys(s.price_sources).join('、')||'未知'}${Object.keys(s.price_sources).length>1?'（混合来源）':''} · 过期价格 ${s.stale_price_attempts} 次 · 档位价格 ${s.tiered_price_attempts} 次`));
    if(value.coverage.incomplete_runs)periodEvidence.append(node('p',`其他账目覆盖不足／总体缺口 ${value.coverage.incomplete_runs} 条 Run：${coverageReasons(value.coverage.reasons)}。原因可重叠，不相加。`));
    periodEvidence.append(node('p','模型 USD 分项不相加称总费用；工具计量另在用量账本按服务／单位查看。'));
    const detail=node('details');detail.append(node('summary','本次期间与来源'));
    detail.append(node('p',`当地日期 [${value.ops_filters.start}, ${value.ops_filters.end})（结束日期不含） · ${value.filters.timezone}`));
    detail.append(node('p',`确定时间 [${value.filters.start}, ${value.filters.end})；先按 Run 开始时间，无开始时间时按受理时间归属。`));
    detail.append(node('p',`精确十进制 USD ${s.exact_usd}（${s.exact_attempts} 次） · 估算十进制 USD ${s.estimated_usd}（${s.estimated_attempts} 次）`));
    detail.append(node('p','包含当前库内所有 Session、用途、准入状态和结局；未关联 Run 的旧消息不计入。Run 的全部已记录 Attempt 消耗随 Run 归属，不是供应商按扣费日分摊的账单。'));
    detail.append(node('p',`持久 Run／Attempt 账目 · 计算 ${time(value.computed_at)} · 有效至 ${time(value.expires_at)}`));
    detail.append(node('p',`来源 ${value.observation_id} · 价格 ${value.price_version} · 实例 ${value.process_instance_id}`));
    const windowLabel=node('p',`本次当地日期 [${value.ops_filters.start}, ${value.ops_filters.end}) · ${value.filters.timezone}`);windowLabel.className='overview-window';
    periodEvidence.append(windowLabel,detail,link('查看本期账目（按原期间重新读取）','/ops?'+new URLSearchParams(value.ops_filters)),node('p','账本会创建新快照；新增记录或价格变化可能改变金额。'));
  }
  /** @param {Wire|null} value */
  function renderMemory(value) {
    memoryBody.replaceChildren();memoryEvidence.replaceChildren();
    const card=metric('已保存的语义／情景条目');memoryBody.append(card);
    if(!value){card.append(metricValue('—'));return;}
    const c=value.counts;
    const total=metricValue(''),prefix=node('span','共 '),suffix=node('span',' 条');
    prefix.className='overview-metric-unit';suffix.className='overview-metric-unit';total.append(prefix,node('span',String(c.total)),suffix);card.append(total);
    if(c.total===0)memoryEvidence.append(node('p','尚无已保存的语义／情景记忆'));
    memoryEvidence.append(node('p',`语义 ${c.semantic} 条 · 情景 ${c.episodic} 条`));
    const details=node('details');details.append(node('summary','本次记忆来源'),node('p',`当前 Store · 修订 ${value.memory_revision} · 实例 ${value.process_instance_id}`),node('p',`读取 ${time(value.observed_at)} · 有效至 ${time(value.expires_at)}`),node('p','同修订完整计数，包含现存受保护条目，不含候选、镜像、旧版本、消息或 Skill 文件。'));
    memoryEvidence.append(details);
  }
  /** Fixed membership and visible display identities are separate. @param {Wire|null} value */
  function visibleRuns(value) {
    const history=(value?.runs||[]).filter((/** @type {Wire} */ row)=>row.run_id!==slot?.run_id);
    return {history,peers:slot?[slot,...history]:history};
  }
  /** @param {Wire|null} value */
  function renderRecent(value) {
    recentBody.replaceChildren();
    if(!value){recentBody.append(node('p','—'));renderCurrent();return;}
    const {history:rows,peers}=visibleRuns(value);
    if(!rows.length)recentBody.append(node('p','暂无可展示的已结束运行'));
    for(const run of rows)recentBody.append(runCard(run,false,peers));
    const details=node('details');details.append(node('summary','本次最近运行来源'),node('p',`持久 Run 索引 · 读取 ${time(value.observed_at)} · 有效至 ${time(value.expires_at)} · 实例 ${value.process_instance_id}`));recentBody.append(details);
    renderCurrent();
  }
  function renderCurrent() {
    const {peers}=visibleRuns(recentSource.value);
    const signature=JSON.stringify([slot,state.connected,peers.map((/** @type {Wire} */ run)=>run.run_id)]);
    if(signature===slotSignature)return;
    slotSignature=signature;current.replaceChildren();
    if(!slot)return;
    current.append(node('h3',state.connected?'当前宿主观测':'断连 · 当前槽最后观测'),runCard(slot,true,peers));
  }
  /** @param {Source} source @param {string} path @param {Record<string,string>} params */
  async function read(source,path,params) {
    source.retire();
    if(disposed||!state.connected||!state.instance){source.stale='等待已核验的实例连接';source.present();return;}
    const instance=state.instance,generation=source.generation;
    const controller=new AbortController();source.request=controller;source.loading=true;source.failure='';source.present();
    try {
      const response=await fetch(path+'?'+new URLSearchParams({process_instance_id:instance,...params}),{signal:controller.signal});
      const body=await response.json();
      if(disposed||generation!==source.generation||instance!==state.instance||!state.connected)return;
      if(!response.ok){
        const reasons=/** @type {Record<string,string>} */({invalid_range:'期间不可用，请重新选择今天、七天或三十天',invalid_timezone:'期间不可用，请填写有效的 IANA 时区后应用',read_quota:'来源资源不足，请稍后重试',memory_changed:'记忆修订已变化，请重新读取',counts_unavailable:'无法证明完整同修订计数',read_unavailable:'无法证明完整读取',process_context_expired:'实例已变化，请刷新'});
        throw new Error(reasons[body.code]||'来源暂不可读');
      }
      if(body.process_instance_id!==instance)throw new Error('来源实例不匹配');
      if(source===memorySource&&body.memory_revision!==state.memoryRevision)throw new Error('记忆修订已变化，请重新读取');
      if(source===recentSource){
        const durable=body.runs.find((/** @type {Wire} */ row)=>row.run_id===slot?.run_id);
        if(slot&&durable?.recording_state==='recorded')slot.recording_state='recorded';
        // The sixth candidate only fills this read's slot exclusion; history then stays fixed.
        body.runs=body.runs.filter((/** @type {Wire} */ row)=>row.run_id!==slot?.run_id).slice(0,5);
        notice.textContent='';
      }
      source.accept(body);restoreFocus();
    }catch(error){if(!disposed&&generation===source.generation){source.loading=false;source.failure=error instanceof Error?error.message:'来源暂不可读';source.present();}}
  }
  function readPeriod(){return read(periodSource,'/api/overview/period',{range,timezone:timezone||''});}
  function readMemory(){if(state.memoryState!=='online'){memorySource.stale='记忆修订尚未核验';memorySource.present();return;}return read(memorySource,'/api/overview/memory-counts',{expected_memory_revision:String(state.memoryRevision)});}
  function readRecent(){return read(recentSource,'/api/overview/recent-runs',{});}
  periodSource.refresh.onclick=()=>void readPeriod();memorySource.refresh.onclick=()=>void readMemory();recentSource.refresh.onclick=()=>void readRecent();
  refreshAll.onclick=()=>{void readPeriod();void readMemory();void readRecent();};
  function changedPeriod(){options.replaceSource('/overview?'+new URLSearchParams({range,timezone:timezone||''}));periodSource.clear();void readPeriod();}
  select.onchange=()=>{range=select.value;changedPeriod();};
  applyZone.onclick=()=>{timezone=zoneInput.value.trim();changedPeriod();};
  renderPeriod(null);renderMemory(null);renderRecent(null);
  let memoryStarted=false;
  const unsubscribe=dashboard.subscribeState(next=>{
    const previous=state;
    state=next;
    const sources=[periodSource,memorySource,recentSource];
    const changedInstance=previous.instance&&state.instance&&previous.instance!==state.instance;
    if(changedInstance){
      for(const source of sources)source.clear('实例已变化，请刷新');
      memoryStarted=true;slot=null;notice.textContent='';
    }
    if(previous.connected&&!state.connected){for(const source of sources){source.retire();source.offline=true;source.stale='连接核验中';source.present();}}
    if(!previous.connected&&state.connected){for(const source of sources){source.offline=false;if(started&&!changedInstance)source.stale='连接已恢复，旧快照待刷新';source.present();}}
    if(previous.memoryRevision!==undefined&&state.memoryRevision!==previous.memoryRevision&&!changedInstance){memorySource.clear('记忆修订已变化，请重新读取');}
    if(previous.memoryState==='online'&&state.memoryState!=='online'){memorySource.retire();memorySource.stale='记忆修订尚未核验';memorySource.present();}
    if(previous.readGapRevision!==undefined&&state.readGapRevision!==previous.readGapRevision){
      for(const source of sources){source.retire();source.stale='事件有缺口，来源待刷新';source.present();}
    }
    if(state.connected){
      const projection=state.projection;
      const active=state.active;
      const candidate=projection&&projection.process_instance_id===state.instance&&projection.state_revision===state.revision?projection:active;
      const oldSlot=slot;
      slot=candidate&&typeof candidate.run_id==='string'?{...candidate}:null;
      if(slot){
        const durable=recentSource.value?.runs.find((/** @type {Wire} */ row)=>row.run_id===slot?.run_id);
        if(durable?.recording_state==='recorded')slot.recording_state='recorded';
        if(oldSlot&&oldSlot.run_id===slot.run_id&&oldSlot.recording_state==='recorded')slot.recording_state='recorded';

      }
      if(oldSlot?.run_id!==slot?.run_id&&recentSource.value)renderRecent(recentSource.value);
      if(started&&!changedInstance&&previous.revision!==undefined&&state.revision!==previous.revision){
        if(periodSource.value){periodSource.stale='有新运行事实，期间快照待刷新';periodSource.present();}
        if(recentSource.value)notice.textContent='有新运行，刷新查看';
      }
    }
    renderCurrent();
    if(!started&&state.connected&&state.instance){started=true;void readPeriod();void readRecent();}
    if(!memoryStarted&&state.connected&&state.memoryState==='online'){memoryStarted=true;void readMemory();}
  });
  return {getLeaveState:()=>({dirty:false}),setVisible:()=>{for(const source of [periodSource,memorySource,recentSource])source.present();},captureSource,restoreSource(/** @type {Wire} */ origin){if(origin?.kind==='overview'){pendingRestore=origin;anchor=origin.anchor;focusHref=origin.focusHref;sectionKey=origin.section;queueMicrotask(restoreFocus);}},dispose(){disposed=true;pendingRestore=null;document.removeEventListener('pointerdown',cancelRestore);document.removeEventListener('keydown',cancelRestore);unsubscribe();for(const source of [periodSource,memorySource,recentSource])source.dispose();}};
}

/** @param {Wire} run @param {boolean} current @param {Wire[]} peers */
function runCard(run,current,peers) {
  const card=node('article');card.className='overview-run sy-row';card.dataset.runId=run.run_id;
  const purposes=/** @type {Record<string,string>} */({chat:'普通聊天',aggregation:'手动聚合',inference_probe:'推理探针',consolidation:'记忆提炼'});
  const outcomes=/** @type {Record<string,string>} */({completed:run.purpose==='chat'&&run.reply_disposition==='reply'?'回复完成':'已完成',max_steps:'受控停止',failed:'受控失败',interrupted:'终态无法确认'});
  const id=run.run_id,short=id.length>16?id.slice(0,12)+'…':id;
  const collides=peers.some(peer=>peer.run_id!==id&&peer.run_id.slice(0,12)===id.slice(0,12));
  const phase=run.phase==='accepted'?'已接受':run.phase==='running'?'运行中':run.phase==='finished'?(run.outcome==='completed'&&run.reply_disposition==='no_reply'?'已结束':outcomes[run.outcome]||'结局待核验'):'阶段待核验';
  const admission=/** @type {Record<string,string>} */({pending:'准入待定',admitted:'已获准',rejected:'未获准',unconfirmed:'准入未确认'});
  const identity=link(`Run ${collides?id:short}`, '/runs/'+encodeURIComponent(run.run_id));identity.className='overview-run-id';
  const preview=node('p',purposes[run.purpose]||`未知用途：${run.purpose??'未知'}`);preview.className='overview-run-preview sy-row-content';
  const states=node('div');states.className='overview-run-states sy-row-meta';
  const outcome=node('span',phase),recording=node('span',run.recording_state==='recorded'?'已保存':run.recording_state==='failed'?'未保存':run.recording_state==='pending'?'正在保存':'记录状态未知');
  outcome.className=run.outcome==='failed'?'sy-tone-error':run.outcome==='completed'?'sy-tone-success':'sy-tone-attention';
  recording.className=run.recording_state==='recorded'?'sy-tone-muted':run.recording_state==='failed'?'sy-tone-error':'sy-tone-attention';
  states.append(outcome,recording);
  const facts=node('div');facts.className='overview-run-facts sy-row-meta';
  facts.append(node('span',`${current?'开始':'受理'}时间：${time(current?run.started_at:run.accepted_at)}`),node('span',`入口：${run.gateway||'未知'}${run.entry_surface_id?` / ${run.entry_surface_id}`:''} · ${admission[run.admission_state]||'准入事实未提供'}`));
  card.append(identity,preview,states,facts);
  if(id.length>16){const full=node('details');full.className='overview-run-detail';full.append(node('summary','完整 Run 标识'),node('p',id));card.append(full);}
  if(run.reply_disposition==='no_reply'){const reply=node('p','已结束 · 按要求未回复');reply.className='overview-run-detail sy-row-meta';card.append(reply);}
  if(run.aggregation){const graph=node('p',`Graph 结局：${run.aggregation.graph_result??'未知'} · ${run.aggregation.reply_disposition==='reply'?'已产出聚合草稿':run.aggregation.reply_disposition==='no_reply'?'无聚合草稿':'草稿事实未知'}`);graph.className='overview-run-detail sy-row-meta';card.append(graph);}
  return card;
}

/** @param {HTMLElement} root */
function renderArchitecture(root) {
  const box=section('静态架构说明');box.classList.add('overview-architecture');
  asPanel(box,'本版本的组件关系；不表示当前配置可用性或某次运行的执行路径');
  const body=node('div');body.className='sy-panel-body';
  const scroll=node('div');scroll.className='overview-diagram-scroll';scroll.tabIndex=0;scroll.setAttribute('role','region');scroll.setAttribute('aria-label','静态架构说明图示（宽度不足时可横向滚动）');
  const relations=document.createElementNS('http://www.w3.org/2000/svg','svg');relations.classList.add('overview-diagram');relations.setAttribute('viewBox','0 0 780 480');relations.setAttribute('role','img');relations.setAttribute('aria-label','CLI 与 Web 入口提交到共享宿主。宿主连接对话处理、显式手动聚合与持久账目；对话和聚合按需使用模型、工具、记忆。');
  // Geometry and marker references belong to this SVG; visual roles live in scoped CSS.
  relations.innerHTML=`
    <defs><marker id="overview-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="overview-arrow" d="M0 0 L10 5 L0 10 Z"/></marker></defs>
    <rect class="overview-partition" x="8" y="30" width="764" height="434"/>
    <text class="overview-diagram-eyebrow" x="20" y="18">ALFRED · 本机组件关系</text>
    <rect class="overview-partition" x="284" y="66" width="220" height="370"/>
    <text class="overview-diagram-eyebrow" x="294" y="54">RUNTIME · 共享宿主</text>
    <rect class="overview-partition" x="530" y="206" width="242" height="230"/>
    <text class="overview-diagram-eyebrow" x="542" y="194">CAPABILITIES · 按需使用</text>
    <path class="overview-edge" marker-end="url(#overview-arrow)" d="M196 128 H300"/>
    <text class="overview-edge-label" x="220" y="116">提交请求</text>
    <path class="overview-edge" marker-end="url(#overview-arrow)" d="M488 128 H616"/>
    <text class="overview-edge-label" x="508" y="116">收尾与持久事实</text>
    <path class="overview-edge" marker-end="url(#overview-arrow)" d="M394 160 V232"/>
    <text class="overview-edge-label" x="404" y="199">对话请求</text>
    <path class="overview-edge overview-edge-optional" marker-end="url(#overview-arrow)" d="M348 160 V180 H252 V390 H300"/>
    <text class="overview-edge-label" x="262" y="325">显式发起</text>
    <path class="overview-edge overview-edge-optional" marker-end="url(#overview-arrow)" d="M488 264 H616"/>
    <text class="overview-edge-label" x="524" y="252">按需使用</text>
    <path class="overview-edge overview-edge-optional" marker-end="url(#overview-arrow)" d="M488 390 H574 V284 H616"/>
    <text class="overview-edge-label" x="498" y="378">按选择使用</text>
    <g class="overview-diagram-node"><rect x="22" y="96" width="174" height="64"/><text x="36" y="122">CLI 入口／Web 入口</text><text class="overview-node-note" x="36" y="144">共享宿主的两个入口</text></g>
    <g class="overview-diagram-node"><rect x="300" y="96" width="188" height="64"/><text x="314" y="122">共享运行宿主与准入</text><text class="overview-node-note" x="314" y="144">接收请求 · 管理运行</text></g>
    <g class="overview-diagram-node"><rect x="300" y="232" width="188" height="64"/><text x="314" y="258">对话处理</text><text class="overview-node-note" x="314" y="280">可选消息分流</text></g>
    <g class="overview-diagram-node"><rect x="300" y="358" width="188" height="64"/><text x="314" y="384">手动聚合</text><text class="overview-node-note" x="314" y="406">由用户显式发起</text></g>
    <g class="overview-diagram-node"><rect x="616" y="232" width="144" height="64"/><text x="630" y="258">模型 · 工具 · 记忆</text><text class="overview-node-note" x="630" y="280">按需／按选择使用</text></g>
    <g class="overview-diagram-node"><rect x="616" y="96" width="144" height="64"/><text x="630" y="122">本地记录与账目</text><text class="overview-node-note" x="630" y="144">持久事实 · 各自核验</text></g>`;
  scroll.append(relations);
  body.append(scroll,node('p','宽度不足时图示可左右滚动；聚焦后使用左右方向键。'),node('p','可选分流、检索门与工具不是每次运行的必经路径；记忆准备可能在图调用之前。Skill 文件与语义／情景条目分开计量。'));
  const links=node('div');links.className='overview-links';
  for(const [label,href] of [['流程与手动聚合','/behaviour'],['模型','/models'],['端点连接','/connections'],['工具','/tools'],['记忆','/memory'],['运行活动','/runs?filter=all'],['用量账本','/ops'],['数据库诊断','/database']])links.append(link(label,href));
  body.append(links);box.append(body);root.append(box);
}
