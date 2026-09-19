import {node} from './dom.js';
/** @typedef {Record<string, any>} Wire */
const names = /** @type {Record<string,string>} */ ({semantic:'语义记忆', episodic:'情景记忆', history:'会话窗口'});
const reasons = /** @type {Record<string,string>} */ ({sources_not_selected:'未选择来源', sources_unavailable:'来源读取失败，无可用资料', capacity_excluded_all:'容量导致无可用资料', sources_excluded_all:'无获准资料', no_matching_sources:'本次有限查询/窗口无资料'});

/** @param {Element} root @param {Wire} facts @param {import("./memory.js").MemorySync} sync */
export function aggregationFacts(root, facts, sync) {
  root.append(node('h3', facts.reply_disposition === 'reply' ? '聚合草稿' : '手动聚合'));
  if (facts.reason_code) root.append(node('p', reasons[facts.reason_code] || facts.reason_code));
  if (facts.error) root.append(node('p', `未生成草稿：${facts.error}`));
  if (facts.recoveries?.length) root.append(node('p', '已降级：部分来源读取失败。'));
  for (const [kind, source] of Object.entries(/** @type {Record<string,Wire>} */ (facts.sources || {}))) {
    const status = source.read_outcome === 'skipped' ? '未选择' : source.read_outcome === 'failed' ? `读取失败 (${source.code})` : source.read_outcome === 'not_started' ? '尚未读取' : '已读取';
    const capacity = source.capacity_excluded == null || source.request_excluded == null ? '未知' : source.capacity_excluded + source.request_excluded;
    const rounds = kind === 'history' ? `轮数排除 ${source.round_excluded ?? '未知'}；` : '';
    root.append(node('p', `${names[kind]}：${status}；实际提供 ${source.actual_input_count ?? '未知'}；容量排除 ${capacity}；${rounds}许可排除 ${source.permission_excluded ?? '未知'}；未取完状态 ${source.remaining ?? 'unknown'}`));
  }
  if (facts.provided?.length) {
    root.append(node('p', '本次提供的资料（不代表逐句证实）'));
    for (const ref of facts.provided) {
      const label = `${names[ref.kind]} ${ref.label}`;
      if (ref.available === false) { root.append(node('p', label + ' · 不可用')); continue; }
      if (ref.kind === 'history') {
        const link = node('a', label); link.href = `/runs/${encodeURIComponent(ref.run_id)}`; root.append(link);
      } else {
        const button = node('button', label); const detail = node('p');
        let serial = 0;
        /** @type {null|(()=>void)} */ let unwatch = null;
        button.addEventListener('click', async () => {
          if (!unwatch) unwatch = sync.watch(() => {
            serial++;
            detail.textContent = sync.online ? '资料已变化，请重新查看。' : '离线或无法核验，原资料已隐藏。';
            if (!button.isConnected) {
              detail.textContent = '';
              unwatch?.(); unwatch = null;
            }
          });
          const request = ++serial;
          detail.textContent = sync.online ? '正在读取原资料…' : '离线或无法核验，原资料已隐藏。';
          if (!sync.online) return;
          const result = await sync.read('/api/memory/record', {kind:ref.kind, id:ref.memory_id});
          if (request !== serial || !button.isConnected || !sync.online || result.state === 'stale') return;
          if (result.state === 'ok' && result.body.record?.id === ref.memory_id && result.body.record.record_version === ref.record_version) {
            detail.textContent = result.body.record.fact || result.body.record.summary || '原资料不可用。';
          } else detail.textContent = '原资料不可用或无法核验。';
        });
        root.append(button, detail);
      }

    }
  }
}

/** @param {HTMLElement} root @param {()=>string} csrf @param {()=>Wire} runtime @param {import("./memory.js").MemorySync} sync */
export function aggregationForm(root, csrf, runtime, sync) {
  const section = node('section'); section.setAttribute('aria-label', '手动聚合');
  const target = node('select'); target.setAttribute('aria-label','目标会话');
  const goal = node('textarea'); goal.setAttribute('aria-label','聚合目标');
  const keywords = node('input'); keywords.setAttribute('aria-label','聚合关键词');
  const choices = /** @type {Map<string,HTMLInputElement>} */ (new Map());
  section.append(node('h2','手动聚合'), node('p','单次动作 · 显式生成时固定目标会话与所选来源，草稿进入目标会话。草稿不自动进入工作窗口、后续聚合或提炼；再次生成会创建新运行。'));
  for (const [text, control] of /** @type {[string, HTMLElement][]} */ ([['目标会话',target],['聚合目标',goal],['聚合关键词',keywords]])) {
    const label = node('label',text); label.append(control); section.append(label);
  }
  for (const [kind, name] of Object.entries(names)) {
    const label = node('label', name); const check = node('input'); check.type = 'checkbox'; check.checked = true;
    choices.set(kind, check); label.prepend(check); section.append(label);
  }
  const send = node('button','生成聚合草稿'); const refresh = node('button','刷新会话');
  const status = node('p'); status.setAttribute('role','status');
  const result = node('div');
  section.append(send, refresh, status, result); root.append(section);
  let pending = false;
  let run = '';
  let uncertain = false;
  let awaiting = false;
  let rendered = '';
  /** Preserve expanded current-source views across unchanged evidence polls. @param {Wire} facts */
  function renderFacts(facts) {
    const identity = JSON.stringify(facts);
    if (identity === rendered) return;
    rendered = identity;
    result.replaceChildren(); aggregationFacts(result, facts, sync);
  }
  let keywordsEdited = false;
  keywords.addEventListener('input', () => { keywordsEdited = true; });
  goal.addEventListener('input', () => { if (!keywordsEdited) keywords.value = goal.value; });
  let sessionRequest = 0;
  async function sessions() {
    const request = ++sessionRequest;
    try {
      const response = await fetch('/api/sessions?limit=100');
      if (!response.ok) throw new Error();
      const body = await response.json();
      if (request !== sessionRequest || !section.isConnected) return;
      const selected = target.value || sessionStorage.getItem('alfred.session');
      target.replaceChildren(node('option','请选择目标会话'));
      target.options[0].value = '';
      for (const item of body.sessions || []) {
        const option = node('option', item.title || item.session_id); option.value = item.session_id; target.append(option);
      }
      if (selected) target.value = selected;
    } catch {
      if (request === sessionRequest && section.isConnected) status.textContent = '会话读取失败，请刷新。';
    }
  }
  async function update() {
    if (!section.isConnected) return;
    try {
      const state = runtime();
      send.disabled = pending || uncertain || awaiting || Boolean(state.active) || !state.connected || state.unavailable;
      if (state.projection?.aggregation) {
        renderFacts(state.projection.aggregation);
        status.textContent = state.projection.recording_state === 'failed' ? '草稿未保存，请查看运行记录状态。' : '正在保存…';
      }
      if (run && !state.projection?.aggregation) {
        const response = await fetch('/api/run-evidence?' + new URLSearchParams({run_id:run}));
        if (response.ok) {
          const evidence = await response.json();
          const facts = evidence.memory?.aggregation;
          if (facts) {
            awaiting = false;
            renderFacts(facts);
            if (!result.querySelector('a[data-run-link]')) {
              const link = node('a','查看同一运行与资料'); link.dataset.runLink = run; link.href = `/runs/${encodeURIComponent(run)}`; result.append(link);
            }
            status.textContent = evidence.recording_state === 'failed' ? '草稿未保存，请查看运行记录状态。' : '本次聚合已结束；草稿请在目标会话查看。';
          } else status.textContent = '正在读取资料或起草…';
        }
      }
    } catch { /* Existing request remains fixed; observation never resubmits. */ }
    if (section.isConnected) setTimeout(() => void update(), 700);
  }
  send.addEventListener('click', async () => {
    if (pending || uncertain) return;
    const body = {purpose:'aggregation', session_id:target.value, message:goal.value,
      keywords:keywords.value, sources:[...choices].filter(([,v]) => v.checked).map(([k]) => k)};
    pending = true; send.disabled = true; status.textContent = '正在提交…';
    try {
      const response = await fetch('/api/runs', {method:'POST', headers:{'Content-Type':'application/json','x-agent-alfred-csrf':csrf()},body:JSON.stringify(body)});
      const value = await response.json();
      if (!response.ok) { status.textContent = `未提交：${value.code}；表单已保留。`; return; }
      run = value.run_id; awaiting = true; status.textContent = '正在读取资料或起草…';
    } catch {
      uncertain = true; status.textContent = '准入未确认；请查看已有运行，确认后刷新页面。不会自动重投。';
      const link = node('a','查看已有运行'); link.href='/runs'; result.append(link);
    } finally { pending = false; }
  });
  refresh.addEventListener('click', () => void sessions());
  void sessions(); void update();
  return section;
}
