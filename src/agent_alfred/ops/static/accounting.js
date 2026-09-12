import { node } from './dom.js';
import { request } from './tools.js';
/** @typedef {Record<string, any>} Wire */
const LABEL = /** @type {Record<string,string>} */ ({confirmed:'已确认启动', not_started:'明确未启动', unconfirmed:'启动未确认', succeeded:'成功', failed:'确定失败', unknown:'结果未知，请核验原操作', not_executed:'未执行', pending:'等待处理', not_billable:'不计费', reported:'已报告计量', unrecorded:'历史未记录'});
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
  for (const value of ['', 'chat', 'inference_probe', 'consolidation']) {const option = node('option', value || '全部用途'); option.value = value; purpose.append(option);}
  const run = node('input'); run.placeholder = '精确 Run ID'; run.value = params.get('run_id') || ''; run.setAttribute('aria-label','Run ID');
  const tool = node('input'); tool.value = params.get('tool') || ''; tool.setAttribute('aria-label','工具身份'); tool.placeholder = '来源与能力身份';
  for (const [key, input] of /** @type {[string, HTMLInputElement|HTMLSelectElement][]} */ ([
    ['range', range], ['timezone', zone], ['start', start], ['end', end],
    ['session_id', session], ['purpose', purpose],
  ])) if (params.has(key)) input.value = params.get(key) || '';
  const refresh = node('button', '刷新账目'); refresh.type = 'submit';
  filters.append(range, zone, start, end, session, purpose, tool, run, refresh);
  const state = node('p'); state.setAttribute('role','status');
  const totals = node('section'); totals.setAttribute('aria-label','账目汇总');
  const rows = node('section'); const detail = node('section'); detail.setAttribute('aria-label','运行账目明细');
  const more = node('button','下一页'); more.hidden = true;
  root.append(filters, state, node('p','工具筛选展示包含该工具的完整 Run；模型费用属于整条 Run。'), totals, rows, more, detail);
  let alive = true, online = true, instance = '', generation = 0, detailGeneration = 0, historyGeneration = 0;
  let snapshot = '', currentRun = '', next = 0, stale = false, refreshing = false;
  const controllers = new Set();
  /** @type {Wire|null} */ let historyQuery = null;
  /** @type {Wire|null} */ let segment = null;
  /** @type {HTMLElement|null} */ let preview = null;
  /** @type {HTMLButtonElement|null} */ let nextSegment = null;
  const historyState = node('p');
  /** @param {string} path @param {Wire|undefined} [body] */
  async function fetchData(path, body) {
    const control = new AbortController(); controllers.add(control);
    try {return await request(path, body, csrf(), control.signal);}
    finally {controllers.delete(control);}
  }
  function clearHistory() {++historyGeneration; historyQuery = null; segment = null; preview?.replaceChildren(); preview = null; nextSegment = null;}
  function expired() {stale = true; more.disabled = true; state.textContent = '旧快照已失效；保留筛选和已加载财务内容，请显式刷新。';}
  /** @param {Wire} value */
  function renderSummary(value) {
    const s = value.summary;
    totals.replaceChildren(node('p', `Run ${s.run_count} · 模型 Attempt ${s.attempt_count} · 账目覆盖不足 Run ${s.incomplete_runs}`),
      node('p', `精确费用 USD ${s.exact_usd} · 估算费用 USD ${s.estimated_usd} · 费用未知 Attempt ${s.unknown_cost_attempts}`),
      node('p', `工具请求 ${s.tool_requests} · 已确认启动 ${s.confirmed_starts} · 明确未启动 ${s.not_started} · 启动未确认 ${s.unconfirmed_starts}`),
      node('p', `计算时刻 ${value.computed_at} · 时区 ${value.filters.timezone} · 有效至 ${value.expires_at}`));
    for (const cost of s.tool_costs) totals.append(node('p', `工具服务 ${cost.service}：${cost.units} ${cost.unit}`));
    totals.append(node('p', `工具计量状态：${JSON.stringify(s.tool_cost_states)}`));
    if (value.unresolved_membership?.length) totals.append(node('p', `范围覆盖不足：${value.unresolved_membership.length} 条 Run 的时间无法判定，未纳入所示汇总。可选择全部历史核对。`));
    const tokens = node('details'); tokens.append(node('summary','Token 用量与缺失覆盖'),node('pre',JSON.stringify(s.tokens,null,2))); totals.append(tokens);
  }
  /** @param {Wire} value */
  function append(value) {
    for (const row of value.runs) {
      const card = node('article'); card.className = 'card';
      const open = node('button', `查看账目 ${row.run_id}`);
      open.onclick = () => void showRun(row.run_id);
      card.append(open, node('p', `${row.purpose} · ${row.phase} · ${row.outcome || '未结束'} · ${row.time_basis === 'unrecorded' ? '时间缺失或损坏' : `${row.at} (${row.time_basis === 'accepted_at' ? '未开始，使用受理时间' : '开始时间'})`}`),
        node('p', row.coverage.length ? '覆盖不足：' + row.coverage.join('、') : '已持久记录'));
      rows.append(card);
    }
    next = value.next_offset; more.hidden = next === null; more.disabled = stale;
  }
  async function refreshView() {
    if (!online || refreshing) return;
    const mine = ++generation;
    refreshing = true; more.disabled = true;
    state.textContent = '正在创建账目快照…'; refresh.disabled = true;
    const body = {range:range.value,timezone:zone.value,start:start.value,end:end.value,
      session_id:session.value,purpose:purpose.value,tool:tool.value,run_id:run.value};
    try {
      const result = await fetchData('/api/ops/snapshots', body);
      if (!alive || mine !== generation) return;
      if (!result.ok) throw new Error(result.value.error?.code || result.value.code || '读取失败');
      snapshot = result.value.snapshot_id; instance = result.value.process_instance_id; stale = false;
      clearHistory(); ++detailGeneration; currentRun = ''; detail.replaceChildren(); rows.replaceChildren();
      renderSummary(result.value); append(result.value); state.textContent = '固定账目快照；后台变化后请显式刷新。';
    } catch (error) {if (alive && mine === generation) state.textContent = `刷新失败：${error instanceof Error ? error.message : "读取失败"}；保留旧内容及筛选。`;}
    finally {if (alive && mine === generation) {refreshing = false; refresh.disabled = false; more.disabled = stale;}}
  }
  more.onclick = async () => {
    if (stale || !online || refreshing || more.disabled) return;
    const mine = generation, requestedSnapshot = snapshot; more.disabled = true;
    try {const result = await fetchData(`/api/ops?snapshot_id=${requestedSnapshot}&offset=${next}`);
      if (!alive || mine !== generation || snapshot !== requestedSnapshot) return;
      if (result.status === 410) return expired();
      if (result.ok) append(result.value); else state.textContent = '翻页读取失败；保留旧快照。';
    } catch {if (alive && mine === generation) state.textContent = '翻页读取失败；保留旧快照。';}
    finally {if (alive && mine === generation) more.disabled = stale || refreshing;}
  };
  /** @param {string} id */
  async function showRun(id) {
    if (stale || !online || refreshing) return;
    const mine = ++detailGeneration; clearHistory();
    currentRun = id;
    try {
      const result = await fetchData('/api/ops/detail?' + new URLSearchParams({snapshot_id:snapshot,run_id:id}));
      if (!alive || mine !== detailGeneration) return;
      if (result.status === 410) return expired();
      if (!result.ok) {state.textContent = '明细读取失败'; return;}
      detail.replaceChildren(node('h2', `Run ${id}`));
      const context = snapshotContext(result.value.filters); context.set('snapshot_id', snapshot);
      const link = node('a','进入运行过程（保留账目快照）'); link.href = `/runs/${encodeURIComponent(id)}?${context}`; detail.append(link);
      for (const attempt of result.value.run.attempts) {
        const item = node('details'); item.append(node('summary',`Attempt ${attempt.attempt_id} · ${attempt.outcome} · ${attempt.cost.state} ${attempt.cost.amount ?? '金额未知'}`),node('pre',JSON.stringify(attempt,null,2))); detail.append(item);
      }
      for (const request of result.value.run.tools) {
        const item = node('article'); item.className = 'card';
        item.append(node('h3',`${request.matches_filter ? '匹配 · ' : ''}${request.tool_name} · ${request.call_id}`),
          node('p',`${request.currently_registered ? '当前已注册' : '当前未注册'} · ${request.source_id ?? '历史来源未记录'} / ${request.capability_id ?? '身份未记录'}`),
          node('p',`${LABEL[request.start_confirmation]} · ${LABEL[request.result] || request.result} · ${request.reason || ''}`),
          node('p',`工具计量 ${JSON.stringify(request.cost)} · ${request.model_delivery === 'not_sent' ? '明确未发送到后续模型' : '保存投影不证明模型已收到'}`));
        const base = {run_id:id,step_index:String(request.step_index),call_id:request.call_id};
        for (const [projection,label] of [['model','展开模型结果投影'],['audit','完整脱敏审计'],['parameters','模型提交参数（脱敏）']]) {
          const button = node('button',label); button.onclick = () => void loadHistory({...base,projection}); item.append(button);
        }
        const verify = node('button','核验原操作当前结果');
        verify.onclick = async () => {if (!alive || !online || currentRun !== id) return;
          const verificationGeneration = detailGeneration;
          try {const r = await fetchData('/api/tools/verification?' + new URLSearchParams(base));
          if (!alive || !online || verificationGeneration !== detailGeneration || currentRun !== id) return;
          const v = r.value; const output = node('p', `当前核验（独立当前读数）：${JSON.stringify(v)}`); item.append(output);
          if (v.related_run) {const related = node('a','查看恢复 Run'); related.href='/ops?run_id='+encodeURIComponent(v.related_run); item.append(related);}
          } catch {if (alive && verificationGeneration === detailGeneration && currentRun === id) item.append(node('p','当前核验读取失败'));}};
        item.append(verify); detail.append(item);
      }
      preview = node('pre'); preview.setAttribute('aria-label','历史正文当前段');
      nextSegment = node('button','下一段'); nextSegment.disabled = true;
      nextSegment.onclick = () => {if (historyQuery && segment?.next_cursor && online) void loadHistory({...historyQuery,cursor:segment.next_cursor});};
      detail.append(historyState, preview, nextSegment);
    } catch {if (alive && mine === detailGeneration) state.textContent='明细读取失败';}
  }
  /** @param {Wire} query */
  async function loadHistory(query) {
    if (!online || !preview) return;
    const mine = ++historyGeneration, detailId = detailGeneration;
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
  else if (csrf()) void refreshView();
  return {
    sync(/** @type {string} */ processId) {
      const wasOffline = !online; online = true;
      if (!explicitRefresh && !snapshot && generation === 0 && csrf()) void refreshView();
      if (instance && instance !== processId) expired();
      else if (snapshot) state.textContent = '可能有新账目；点击刷新统一重算。';
      if (wasOffline && historyQuery && segment) void loadHistory({...historyQuery,cursor:segment.revalidate_cursor});
    },
    disconnect() {online = false; ++historyGeneration; ++detailGeneration; for (const c of controllers) c.abort();
      if (nextSegment) nextSegment.disabled = true; historyState.textContent = '离线副本：保留当前段，重连核验前不可续读。';},
    close() {alive = false; ++generation; ++detailGeneration; clearHistory(); for (const c of controllers) c.abort();},
  };
}
