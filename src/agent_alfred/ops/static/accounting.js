import { node } from './dom.js';
import { request } from './tools.js';
import { dashboard } from './app.js';
/** @typedef {Record<string, any>} Wire */
const LABEL = /** @type {Record<string,string>} */ ({confirmed:'已确认启动', not_started:'明确未启动', unconfirmed:'启动未确认', succeeded:'成功', failed:'确定失败', unknown:'结果未知，请核验原操作', not_executed:'未执行', pending:'等待处理', not_billable:'不计费', reported:'已报告计量', unrecorded:'历史未记录'});
const PURPOSE = /** @type {Record<string,string>} */ ({chat:'普通聊天', aggregation:'手动聚合', inference_probe:'模型推理探针', consolidation:'记忆提炼'});
const TOKEN = /** @type {Record<string,string>} */ ({total_input_tokens:'输入总量',uncached_input_tokens:'未缓存输入',cache_read_tokens:'缓存读取',cache_write_tokens:'缓存写入',output_tokens:'输出',reasoning_tokens:'推理'});
const COVERAGE = /** @type {Record<string,string>} */ ({telemetry_unreadable_or_missing:'账目缺失或不可读',attempt_count_unconfirmed:'Attempt 数量未确认',recording_pending:'账目记录尚未落定',recording_failed:'账目记录失败',historic_tool_metering_unrecorded:'历史工具计量未记录',time_unrecorded:'时间缺失或损坏',damaged_attempt:'Attempt 账目损坏'});
const BILLING = ['uncached_input','cache_read','cache_write','output'];
/** @param {Wire} cost */
function costText(cost){return cost.state==='exact'?`精确费用 USD ${cost.amount}`:cost.state==='estimated'?`估算费用 USD ${cost.amount}`:'费用未知';}
/** @param {Wire} cost */
function toolCostText(cost){return cost.kind==='reported'?`已报告计量 · 服务 ${cost.service} · ${cost.units} ${cost.unit} · 来源 ${cost.source}`:`${LABEL[cost.kind]||'计量未知'}${cost.reason?' · '+cost.reason:''}`;}
/** @param {Wire} value */
function traceText(value){return [value.trace_incomplete===true?'过程记录不完整':value.trace_incomplete===false?'未报告过程缺口':'过程完整性未记录',value.prune_reason?'正文已裁剪：'+value.prune_reason:''].filter(Boolean).join(' · ');}
/** @param {Wire} attempt */
function attemptDetails(attempt){
  const box=node('div'),usage=table(`Token ${attempt.attempt_id}`,['维度','Token']);
  for(const [key,label] of Object.entries(TOKEN))tableRow(usage.body,[label,attempt.usage[key]??'未报告']);
  box.append(usage.region);
  if(attempt.cost.state==='estimated'){
    const prices=table(`四维价格 ${attempt.attempt_id}`,['维度','Token','USD／百万 Token','价格来源','分项 USD','价格证据'],'ops-prices');
    for(const dimension of BILLING){
      const part=attempt.cost.price_components.find(/** @param {Wire} component */component=>component.dimension===dimension);
      if(part)tableRow(prices.body,[TOKEN[dimension+'_tokens'],part.tokens,part.unit_price,part.source,part.amount,[part.stale?'价格已陈旧':'',part.tiered?'阶梯价':'',part.catalog_fetched_at?'目录获取 '+part.catalog_fetched_at:''].filter(Boolean).join(' · ')||'无额外标记']);
    }
    box.append(prices.region);
  }
  box.append(node('p',attempt.cost.state==='exact'?'精确费用来自端点报告，无需重新估算。':'未提供的价格或 Token 不等于零；四维分项只展示快照中已提供的证据。'));
  return disclosure(`Token 与四维价格：${attempt.attempt_id}`,box);
}
/** @param {string} title @param {string[]} headings @param {string} [kind] */
function table(title, headings,kind='') {
  const region=node('div');region.className='table-scroll '+kind;region.tabIndex=0;region.setAttribute('role','region');region.setAttribute('aria-label',title);
  const element=node('table'),caption=node('caption',title),head=node('thead'),row=node('tr'),body=node('tbody');
  for(const text of headings){const th=node('th',text);th.scope='col';row.append(th);}
  head.append(row);element.append(caption,head,body);region.append(element);return {region,body};
}
/** @param {HTMLElement} body @param {(string|number|HTMLElement)[]} values */
function tableRow(body,values){const row=node('tr');for(const value of values){const cell=node('td');cell.append(value instanceof HTMLElement?value:String(value));row.append(cell);}body.append(row);return row;}
/** @param {string} title @param {HTMLElement} content */
function disclosure(title,content){const box=node('details');box.append(node('summary',title),content);return box;}
/** @param {string} title @param {HTMLInputElement|HTMLSelectElement} input @param {string} [hint] */
function field(title,input,hint=''){const label=node('label');label.append(node('span',title),input);if(hint)label.append(node('small',hint));return label;}
/** @param {Wire} value */
function filterDescription(value){return [value.range==='all'?'全部历史':`半开期间 [${value.start}, ${value.end})`, `时区 ${value.timezone}`,`Session ${value.session_id||'全部'}`,`用途 ${PURPOSE[value.purpose]||value.purpose||'全部'}`,`工具身份 ${value.tool||'全部'}`,`精确 Run ${value.run_id||'全部'}`].join(' · ');}
/** Preserve the normalized snapshot interval, including across a local midnight.
 * @param {Wire} filters */
function snapshotContext(filters) {
  const params = new URLSearchParams();
  for (const key of ['timezone', 'session_id', 'purpose', 'tool', 'run_id'])
    if (filters[key]) params.set('ops_' + key, filters[key]);
  params.set('ops_range', filters.range === 'all' ? 'all' : 'custom');
  const formatter = new Intl.DateTimeFormat('en', {
    timeZone: filters.timezone, year: 'numeric', month: '2-digit', day: '2-digit',
  });
  for (const key of ['start', 'end']) if (filters[key]) {
    const parts = formatter.formatToParts(new Date(filters[key]));
    params.set('ops_' + key, ['year', 'month', 'day'].map(
      name => parts.find(part => part.type === name)?.value || '',
    ).join('-'));
  }
  return params;
}
/** @param {HTMLElement} root @param {()=>string} csrf */
export function accountingPage(root, csrf) {
  root.classList.add('accounting');
  const params = new URLSearchParams(location.search);
  const explicitRefresh = params.has('snapshot_context');
  const filters = node('form'); filters.setAttribute('aria-label', '账目筛选');
  const range = node('select'); range.setAttribute('aria-label', '时间范围');
  for (const [key, label] of [['7d','近七个自然日'],['today','今天'],['30d','近三十个自然日'],['custom','自定义半开区间'],['all','全部历史']]) {
    const option = node('option', label); option.value = key; range.append(option);
  }
  const zone = node('input'); zone.value = Intl.DateTimeFormat().resolvedOptions().timeZone; zone.setAttribute('aria-label', 'IANA 时区');
  const start = node('input'); start.type = 'date'; start.setAttribute('aria-label', '开始日期（含）');
  const end = node('input'); end.type = 'date'; end.setAttribute('aria-label', '结束日期（不含）');
  const session = node('input'); session.placeholder = '会话 ID'; session.setAttribute('aria-label', '会话 ID');
  const purpose = node('select'); purpose.setAttribute('aria-label', '运行用途');
  for (const value of ['', 'chat', 'aggregation', 'inference_probe', 'consolidation']) {const option = node('option', PURPOSE[value] || '全部用途'); option.value = value; purpose.append(option);}
  if(params.get('purpose')&&!Array.from(purpose.options).some(option=>option.value===params.get('purpose'))){const option=node('option',`其他用途：${params.get('purpose')}`);option.value=params.get('purpose')||'';purpose.append(option);}
  const run = node('input'); run.placeholder = '精确 Run ID'; run.value = params.get('run_id') || ''; run.setAttribute('aria-label','Run ID');
  const tool = node('input'); tool.value = params.get('tool') || ''; tool.setAttribute('aria-label','工具身份'); tool.placeholder = '来源与能力身份';
  for (const [key, input] of /** @type {[string, HTMLInputElement|HTMLSelectElement][]} */ ([
    ['range', range], ['timezone', zone], ['start', start], ['end', end],
    ['session_id', session], ['purpose', purpose],
  ])) if (params.has(key)) input.value = params.get(key) || '';
  const refresh = node('button', '刷新账目'); refresh.type = 'submit';
  const fields=node('div');fields.className='ops-filter-fields';
  fields.append(field('时间范围',range),field('IANA 时区',zone),field('开始日期（含）',start,'自定义期间使用当地日期'),field('结束日期（不含）',end,'结束日为排他边界'),field('会话 ID',session),field('运行用途',purpose),field('工具身份',tool,'包含匹配工具的完整 Run'),field('Run ID',run,'使用完整不透明标识'));
  const draftState=node('p');draftState.setAttribute('role','status');
  filters.append(node('h2','待查询条件'),fields,draftState,refresh);
  const state = node('p'); state.setAttribute('role','status');
  const notice=node('p');notice.setAttribute('role','status');
  const retrySource=node('button','重新核验原快照');retrySource.hidden=true;
  const scope=node('section');scope.setAttribute('aria-label','当前账目快照');
  const totals = node('section'); totals.setAttribute('aria-label','账目汇总');
  const rows = node('section'); const detail = node('section'); detail.setAttribute('aria-label','运行账目明细');
  let runTable=table('Run 账目索引',['Run / Session','用途与状态','归属时间','账目与过程覆盖','操作'],'ops-runs');
  const more = node('button','下一页'); more.hidden = true;
  root.append(filters, state, notice, retrySource, scope, node('p','工具筛选展示包含该工具的完整 Run；模型 USD 与工具服务／单位分列，不能归因给单一工具。'), totals, rows, more, detail);
  const runtime=dashboard.runtime();
  let alive = true, online = runtime.connected, instance = runtime.instance, generation = 0, detailGeneration = 0, historyGeneration = 0;
  let snapshot = '', snapshotInstance='', currentRun = '', next = 0, stale = false, refreshing = false, paging=false, begun=false;
  let pageOffset=0,focusArmed=false,detailReadGeneration=0,sourceReadPending=false;
  /** @type {Wire|null} */ let fixedFilters=null;
  /** @type {Wire|null} */ let pendingRestore=null;
  /** @type {Wire|null} */ let restoreSeed=null;
  /** @type {Wire} */ let trigger={kind:'row',run_id:''};
  const rowOffsets=new Map();
  function draft(){return {range:range.value,timezone:zone.value,start:start.value,end:end.value,session_id:session.value,purpose:purpose.value,tool:tool.value,run_id:run.value};}
  let submitted=JSON.stringify(draft());
  function describeDraft(){draftState.textContent=JSON.stringify(draft())===submitted?'筛选只在点击“刷新账目”时应用。':'筛选草稿已修改，尚未应用；当前账目仍使用原快照范围。';}
  filters.addEventListener('input',describeDraft);filters.addEventListener('change',describeDraft);describeDraft();
  const controllers = new Set();
  /** @type {Wire|null} */ let historyQuery = null;
  /** @type {Wire|null} */ let segment = null;
  /** @type {HTMLElement|null} */ let preview = null;
  /** @type {HTMLButtonElement|null} */ let nextSegment = null;
  const historyState = node('p');
  function controls(){refresh.disabled=!online||refreshing;more.disabled=!online||stale||refreshing||paging;retrySource.disabled=!online||refreshing;for(const button of rows.querySelectorAll('button'))button.disabled=!online||stale||refreshing;for(const button of detail.querySelectorAll('button[data-current-read]'))if(button instanceof HTMLButtonElement)button.disabled=!online;}
  function captureSource(){
    if(!snapshot||!fixedFilters)return {};
    const query=new URLSearchParams();for(const [key,value] of snapshotContext(fixedFilters))query.set(key.slice(4),value);
    query.set('snapshot_id',snapshot);
    return {kind:'ops',route:'/ops?'+query,snapshot_id:snapshot,process_instance_id:snapshotInstance,
      filters:{...fixedFilters},offset:rowOffsets.get(currentRun)??pageOffset,run_id:currentRun,trigger:{...trigger}};
  }
  const cancelRestore=()=>{pendingRestore=null;};
  const focusChanged=()=>{if(focusArmed)cancelRestore();};
  document.addEventListener('pointerdown',cancelRestore);document.addEventListener('keydown',cancelRestore);document.addEventListener('focusin',focusChanged);
  root.addEventListener('click',event=>{
    if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
    const link=event.target instanceof Element?event.target.closest('a'):null;
    if(!link||link.origin!==location.origin||link.target)return;
    trigger={kind:link.dataset.sourceKey||'link',run_id:currentRun,href:link.getAttribute('href')};
    event.preventDefault();void dashboard.navigate(link.href,{source:{returnSource:captureSource()}});
  });
  function restoreFocus(){
    const source=pendingRestore;if(!source)return;
    const target=source.trigger?.kind==='detail-link'
      ? detail.querySelector('a[data-source-key="detail-link"]')
      : [...rows.querySelectorAll('button')].find(button=>button.dataset.runId===source.run_id);
    if(target instanceof HTMLElement){pendingRestore=null;target.focus({preventScroll:true});}
  }
  /** @param {string} path @param {Wire|undefined} [body] */
  async function fetchData(path, body) {
    const control = new AbortController(); controllers.add(control);
    try {return await request(path, body, csrf(), control.signal);}
    finally {controllers.delete(control);}
  }
  function clearHistory(retireTargets=true) {
    ++historyGeneration;historyQuery=null;segment=null;preview?.replaceChildren();historyState.textContent='';
    if(nextSegment)nextSegment.disabled=true;
    if(retireTargets){preview=null;nextSegment=null;}
  }
  function expired(restored=false) {stale = true;retrySource.hidden=true;controls();state.textContent = restored?'原账目快照已失效；已保留固定范围，请核对筛选并显式刷新。':'旧快照已失效；保留筛选和已加载财务内容，请显式刷新。';}
  /** @param {Wire} value */
  function renderSummary(value) {
    const s = value.summary;
    scope.replaceChildren(node('h2','当前账目快照'),node('p',filterDescription(value.filters)),
      node('p',`计算时刻 ${value.computed_at} · 有效至 ${value.expires_at}`),
      disclosure('快照与价格身份',node('p',`snapshot_id ${value.snapshot_id} · process_instance_id ${value.process_instance_id} · price_version ${value.price_version}`)));
    totals.replaceChildren(node('h2','分项汇总'),node('p', `Run ${s.run_count} · 模型 Attempt ${s.attempt_count} · 账目覆盖不足 Run ${s.incomplete_runs}`),
      node('p', `精确费用 USD ${s.exact_usd} · 估算费用 USD ${s.estimated_usd} · 费用未知 Attempt ${s.unknown_cost_attempts}`),
      node('p', `工具请求 ${s.tool_requests} · 已确认启动 ${s.confirmed_starts} · 明确未启动 ${s.not_started} · 启动未确认 ${s.unconfirmed_starts}`));
    totals.append(node('p',`精确 Attempt ${s.exact_attempts} · 估算 Attempt ${s.estimated_attempts} · 使用陈旧价格 ${s.stale_price_attempts} · 涉及阶梯价 ${s.tiered_price_attempts}`),node('p',`估算价格来源：${Object.entries(s.price_sources).map(([name,count])=>`${name} ${count}`).join(' · ')||'无'}`));
    for (const cost of s.tool_costs) totals.append(node('p', `工具服务 ${cost.service}：${cost.units} ${cost.unit}`));
    totals.append(node('p', `工具计量状态：${Object.entries(s.tool_cost_states).map(([key,count])=>`${LABEL[key]||key} ${count}`).join(' · ')}`));
    if (value.unresolved_membership?.length) totals.append(node('p', `范围覆盖不足：${value.unresolved_membership.length} 条 Run 的时间无法判定，未纳入所示汇总。可选择全部历史核对。`));
    const tokens=table('Token 用量与缺失覆盖',['维度','已记录 Token','未报告的 Attempt 数']);
    for(const [key,value] of Object.entries(/** @type {Record<string,Wire>} */(s.tokens)))tableRow(tokens.body,[TOKEN[key]||key,value.known,value.missing_attempts]);
    totals.append(disclosure('Token 用量与缺失覆盖',tokens.region),node('p','仅统计持久 Run 与可读账目；无 Run 的旧消息不计入。账目覆盖不证明正式回复已保存，完整记录状态请进入运行过程核对。'));
  }
  /** @param {Wire} value */
  function append(value,offset=0) {
    pageOffset=offset;
    if(!rows.children.length){runTable=table('Run 账目索引',['Run / Session','用途与状态','归属时间','账目与过程覆盖','操作'],'ops-runs');rows.append(runTable.region);}
    for (const row of value.runs) {
      const open = node('button', `查看账目 ${row.run_id}`);
      open.dataset.runId=row.run_id;rowOffsets.set(row.run_id,offset);
      open.onclick = () => {trigger={kind:'row',run_id:row.run_id};void showRun(row.run_id);};
      tableRow(runTable.body,[`${row.run_id} / ${row.session_id||'无 Session'}`,`${PURPOSE[row.purpose]||row.purpose} · ${row.phase} · ${row.outcome||'未结束'}`,row.time_basis==='unrecorded'?'时间缺失或损坏':`${row.at}（${row.time_basis==='accepted_at'?'未开始，使用受理时间':'开始时间'}）`,`${row.coverage.length?'账目覆盖不足：'+row.coverage.map(/** @param {string} code */code=>COVERAGE[code]||code).join('、'):'账目覆盖无已报告缺口'} · ${traceText(row)}`,open]);
    }
    next = value.next_offset; more.hidden = next === null; controls();
  }
  async function refreshView() {
    if (!online || refreshing) return;
    begun=true;pendingRestore=null;retrySource.hidden=true;restoreSeed=null;sourceReadPending=false;
    const mine = ++generation, requestedInstance=instance;
    refreshing = true; more.disabled = true;
    state.textContent = '正在创建账目快照…'; refresh.disabled = true;
    const body = draft();
    try {
      const result = await fetchData('/api/ops/snapshots', body);
      if (!alive || !online || mine !== generation || requestedInstance!==instance) return;
      if (!result.ok) throw new Error(result.value.error?.code || result.value.code || '读取失败');
      if(result.value.process_instance_id!==requestedInstance)throw new Error('来源实例不匹配');
      snapshot = result.value.snapshot_id;snapshotInstance=result.value.process_instance_id;fixedFilters=result.value.filters;stale = false;
      submitted=JSON.stringify(body);describeDraft();
      clearHistory(); ++detailGeneration; currentRun = ''; detail.replaceChildren(); rows.replaceChildren();rowOffsets.clear();
      renderSummary(result.value); append(result.value);notice.textContent='';state.textContent = '固定账目快照；后台变化后请显式刷新。';
    } catch (error) {if (alive && mine === generation) state.textContent = `刷新失败：${error instanceof Error ? error.message : "读取失败"}；保留旧内容及筛选。`;}
    finally {if (alive && mine === generation) {refreshing = false;controls();}}
  }
  more.onclick = async () => {
    if (stale || !online || refreshing || more.disabled) return;
    const mine = generation, requestedSnapshot = snapshot,offset=next;paging=true;controls();
    try {const result = await fetchData('/api/ops?'+new URLSearchParams({snapshot_id:requestedSnapshot,offset:String(offset)}));
      if (!alive || !online || stale || mine !== generation || snapshot !== requestedSnapshot) return;
      if (result.status === 410) return expired();
      if (result.ok&&result.value.snapshot_id===requestedSnapshot&&result.value.process_instance_id===snapshotInstance) append(result.value,offset); else state.textContent = '翻页读取失败；保留旧快照。';
    } catch {if (alive && mine === generation) state.textContent = '翻页读取失败；保留旧快照。';}
    finally {if (alive && mine === generation) {paging=false;controls();}}
  };
  /** @param {string} id */
  async function showRun(id) {
    if (stale || !online || refreshing) return;
    const read=++detailReadGeneration,requestedSnapshot=snapshot,requestedInstance=instance;
    if(currentRun!==id){++detailGeneration;clearHistory();detail.replaceChildren(node('h2',`Run ${id}`),node('p','正在读取所选 Run 账目…'));}
    const mine=detailGeneration;
    currentRun = id;
    try {
      const result = await fetchData('/api/ops/detail?' + new URLSearchParams({snapshot_id:snapshot,run_id:id}));
      if (!alive || !online || stale || read!==detailReadGeneration || mine !== detailGeneration || snapshot!==requestedSnapshot||instance!==requestedInstance) return;
      if (result.status === 410) return expired();
      if (!result.ok) {state.textContent = '明细读取失败'; return;}
      if(result.value.snapshot_id!==requestedSnapshot||result.value.process_instance_id!==snapshotInstance||result.value.run.run_id!==id){state.textContent='明细来源身份不匹配';return;}
      clearHistory();++detailGeneration;
      detail.replaceChildren(node('h2', `Run ${id}`));
      const context = snapshotContext(result.value.filters); context.set('snapshot_id', snapshot);
      const link = node('a','进入运行过程（保留账目快照）');link.dataset.sourceKey='detail-link';link.href = `/runs/${encodeURIComponent(id)}?${context}`; detail.append(link);
      detail.append(node('p',`所属 snapshot_id ${requestedSnapshot}`),node('p',traceText(result.value.run)));
      const attempts=table('模型 Attempt',['Attempt','模型身份','结果','费用','用量摘要'],'ops-attempts');
      for (const attempt of result.value.run.attempts) {
        tableRow(attempts.body,[attempt.attempt_id,`${attempt.model.endpoint_id??'端点未记录'} / ${attempt.model.model_id??'模型未记录'}`,attempt.outcome,costText(attempt.cost),`输入总量 ${attempt.usage.total_input_tokens??'未报告'} · 输出 ${attempt.usage.output_tokens??'未报告'}`]);
      }
      detail.append(attempts.region);
      for(const attempt of result.value.run.attempts)detail.append(attemptDetails(attempt));
      for (const request of result.value.run.tools) {
        const item = node('article'); item.className = 'card';
        item.append(node('h3',`${request.matches_filter ? '匹配 · ' : ''}${request.tool_name} · ${request.call_id}`),
          node('p',`${request.currently_registered ? '当前已注册' : '当前未注册'} · ${request.source_id ?? '历史来源未记录'} / ${request.capability_id ?? '身份未记录'}`),
          node('p',`当次结果：${LABEL[request.start_confirmation]} · ${LABEL[request.result] || request.result} · ${request.reason || ''}`),
          node('p',`工具计量 ${toolCostText(request.cost)} · ${request.model_delivery === 'not_sent' ? '明确未发送到后续模型' : '保存投影不证明模型已收到'}`));
        const base = {run_id:id,step_index:String(request.step_index),call_id:request.call_id};
        for (const [projection,label] of [['model','展开模型结果投影'],['audit','完整脱敏审计'],['parameters','模型提交参数（脱敏）']]) {
          const button = node('button',label);button.dataset.currentRead=''; button.onclick = () => void loadHistory({...base,projection}); item.append(button);
        }
        const verify = node('button','核验原操作当前结果');
        verify.dataset.currentRead='';
        verify.onclick = async () => {if (!alive || !online || currentRun !== id) return;
          const verificationGeneration = detailGeneration;
          try {const r = await fetchData('/api/tools/verification?' + new URLSearchParams(base));
          if (!alive || !online || verificationGeneration !== detailGeneration || currentRun !== id) return;
          if(!r.ok){item.append(node('p','当前核验读取失败：'+(r.value.error?.code||r.value.code||'读取失败')));return;}
          const v = r.value; const output = node('section');output.setAttribute('aria-label','当前核验');output.append(node('h4','当前核验（独立当前读数）'),node('p',`状态 ${LABEL[v.state]||v.state} · 读取时刻 ${v.observed_at}`),node('p',`操作 ${v.operation_id||'未记录'} · 核验时刻 ${v.verified_at||'未记录'} · 证据来源 ${v.evidence_source||'未记录'}`));item.append(output);
          if (v.related_run) {const related = node('a','查看恢复 Run'); related.href='/ops?run_id='+encodeURIComponent(v.related_run); output.append(related);}
          } catch {if (alive && verificationGeneration === detailGeneration && currentRun === id) item.append(node('p','当前核验读取失败'));}};
        item.append(verify); detail.append(item);
      }
      preview = node('pre'); preview.setAttribute('aria-label','历史正文当前段');
      preview.tabIndex=0;
      nextSegment = node('button','下一段'); nextSegment.disabled = true;
      nextSegment.onclick = () => {if (historyQuery && segment?.next_cursor && online) void loadHistory({...historyQuery,cursor:segment.next_cursor});};
      detail.append(historyState, preview, nextSegment);
      controls();restoreFocus();
    } catch {if (alive && read===detailReadGeneration && mine === detailGeneration) state.textContent='明细读取失败';}
  }
  /** Restore only safe source identities and normalized filters, never financial/body copies. @param {Wire} source */
  function restoreSource(source){
    if(source?.kind!=='ops'||typeof source.snapshot_id!=='string'||!source.snapshot_id)return;
    begun=true;restoreSeed=source;pendingRestore=source;focusArmed=false;
    queueMicrotask(()=>{focusArmed=true;});
    snapshot=source.snapshot_id;snapshotInstance=source.process_instance_id||instance;
    fixedFilters=source.filters||null;trigger=source.trigger||{kind:'row',run_id:source.run_id};
    if(fixedFilters){
      const normalized=snapshotContext(fixedFilters);
      for(const [key,input] of /** @type {[string,HTMLInputElement|HTMLSelectElement][]} */([['range',range],['timezone',zone],['start',start],['end',end],['session_id',session],['purpose',purpose],['tool',tool],['run_id',run]]))input.value=normalized.get('ops_'+key)||'';
      submitted=JSON.stringify(draft());describeDraft();
      scope.replaceChildren(node('h2','原账目来源待核验'),node('p',filterDescription(fixedFilters)),node('p',`snapshot_id ${snapshot}`));
    }
    void readSource(source);
  }
  /** @param {Wire} source */
  async function readSource(source){
    if(!alive||!online){state.textContent='断连；保留原快照身份，连接后再核验。';retrySource.hidden=false;return;}
    if(snapshotInstance&&snapshotInstance!==instance){expired(true);return;}
    const mine=++generation,requestedSnapshot=snapshot,requestedInstance=instance;
    refreshing=true;sourceReadPending=true;controls();state.textContent='正在核验原账目快照…';
    const offset=Number.isSafeInteger(source.offset)&&source.offset>=0?source.offset:0;
    try{
      const result=await fetchData('/api/ops?'+new URLSearchParams({snapshot_id:requestedSnapshot,offset:String(offset)}));
      if(!alive||!online||mine!==generation||instance!==requestedInstance)return;
      if(result.status===410){expired(true);return;}
      if(!result.ok)throw new Error(result.value.error?.code||result.value.code||'读取失败');
      if(result.value.snapshot_id!==requestedSnapshot||result.value.process_instance_id!==requestedInstance)throw new Error('来源实例不匹配');
      snapshotInstance=requestedInstance;fixedFilters=result.value.filters;stale=false;rows.replaceChildren();rowOffsets.clear();renderSummary(result.value);append(result.value,offset);
      retrySource.hidden=true;state.textContent='已核验原账目快照；范围与价格保持不变。';
      refreshing=false;controls();
      if(typeof source.run_id==='string'&&source.run_id)await showRun(source.run_id);else restoreFocus();
    }catch(error){if(alive&&mine===generation){state.textContent=`原快照核验失败：${error instanceof Error?error.message:'读取失败'}；未创建替代账目。`;retrySource.hidden=false;}}
    finally{if(alive&&mine===generation){refreshing=false;sourceReadPending=false;controls();}}
  }
  retrySource.onclick=()=>{if(restoreSeed)void readSource(restoreSeed);};
  /** @param {Wire} query */
  async function loadHistory(query) {
    if (!online || !preview) return;
    const mine = ++historyGeneration, detailId = detailGeneration;
    const same=historyQuery&&['run_id','step_index','call_id','projection'].every(key=>historyQuery?.[key]===query[key]);
    if(!same){segment=null;preview.replaceChildren();}
    historyQuery = query; historyState.textContent = '核验并读取历史…';
    if (nextSegment) nextSegment.disabled = true;
    try {
      const result = await fetchData('/api/tools/history?' + new URLSearchParams(query));
      if (!alive || !online || mine !== historyGeneration || detailId !== detailGeneration) return;
      if (!result.ok) {preview?.replaceChildren(); segment = null; historyState.textContent = '历史不可读：' + (result.value.error?.code || '读取失败'); return;}
      segment = result.value; if (!segment) return; preview.textContent = segment.text;
      historyState.textContent = `${query.projection} · 字节 [${segment.start}, ${segment.end}) / ${segment.total_bytes} · 人工历史，只保留当前段`;
      if (nextSegment) nextSegment.disabled = !segment.next_cursor;
    } catch {if (alive && mine === historyGeneration) historyState.textContent = '历史读取失败，当前段不可继续加载。';}
  }
  filters.onsubmit = event => {event.preventDefault(); void refreshView();};
  if (explicitRefresh) state.textContent = params.get('snapshot_context') === 'expired'
    ? '账目快照已失效；已保留原筛选，请显式刷新。'
    : '账目快照已失效；原筛选上下文未记录，请重新选择并显式刷新。';
  function sync(/** @type {string} */ processId) {
      const wasOffline = !online; online = true;
      const changed=instance&&instance!==processId;instance=processId;
      if(changed){++generation;++detailGeneration;refreshing=false;paging=false;for(const c of controllers)c.abort();clearHistory(false);historyState.textContent='实例已变化；历史正文需重新核验。';if(snapshot)expired();}
      else if(wasOffline&&historyQuery&&segment)void loadHistory({...historyQuery,cursor:segment.revalidate_cursor});
      if(wasOffline)notice.textContent='连接已恢复；账目仍为固定快照，重新核验当前正文后才能续读。';
      if(!explicitRefresh&&!params.has('snapshot_id')&&!begun&&!snapshot&&csrf())queueMicrotask(()=>{if(alive&&!begun)void refreshView();});
      controls();
  }
  function disconnect(){
    if(!online)return;online=false;++generation;++historyGeneration;++detailGeneration;refreshing=false;paging=false;
    if(sourceReadPending){sourceReadPending=false;retrySource.hidden=false;state.textContent='原快照核验被断连中止；连接后可重新核验。';}
    for(const c of controllers)c.abort();controls();
    notice.textContent='连接中断；财务内容为离线快照，暂停读取。';
    if(nextSegment)nextSegment.disabled=true;historyState.textContent='离线副本：保留当前段，重连核验前不可续读。';
  }
  let lastRevision=/** @type {number|undefined} */(undefined);
  const unsubscribe=dashboard.subscribeState(value=>{
    if(!value.connected){disconnect();return;}
    sync(value.instance);
    if(snapshot&&lastRevision!==undefined&&value.revision!==lastRevision)notice.textContent='可能有新账目；点击刷新统一重算。';
    lastRevision=value.revision;
  });
  if(params.has('snapshot_id'))queueMicrotask(()=>{if(alive&&!begun)restoreSource({kind:'ops',snapshot_id:params.get('snapshot_id'),process_instance_id:instance,offset:0});});
  queueMicrotask(()=>{if(alive&&!begun&&!explicitRefresh&&csrf()&&online)void refreshView();});
  controls();
  return {sync,disconnect,captureSource,restoreSource,
    close() {alive = false;pendingRestore=null;unsubscribe();document.removeEventListener('pointerdown',cancelRestore);document.removeEventListener('keydown',cancelRestore);document.removeEventListener('focusin',focusChanged);++generation;++detailGeneration;clearHistory();for(const c of controllers)c.abort();},
  };
}
