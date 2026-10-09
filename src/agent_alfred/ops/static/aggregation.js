import {Drafts} from "./shell.js";
import {dashboard} from './app.js';
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

/** @param {HTMLElement} root @param {()=>string} csrf @param {()=>Wire} runtime @param {import('./memory.js').MemorySync} sync */
export function aggregationForm(root, csrf, runtime, sync) {
  const section = node('section'); section.className = 'card behaviour-aggregation'; section.setAttribute('aria-label','手动聚合');
  const drafts = new Drafts();
  const target = node('select'); target.setAttribute('aria-label','目标会话');
  const goal = node('textarea'); goal.setAttribute('aria-label','聚合目标');
  const keywords = node('input'); keywords.setAttribute('aria-label','聚合关键词');
  const choices = /** @type {Map<string,HTMLInputElement>} */ (new Map());
  section.append(node('h2','手动聚合'),node('p','单次动作 · 显式生成时固定目标会话与所选来源，草稿进入目标会话。草稿不自动进入工作窗口、后续聚合或提炼；再次生成会创建新运行。'));
  for (const [text, control] of /** @type {[string,HTMLElement][]} */ ([['目标会话',target],['聚合目标',goal],['聚合关键词',keywords]])) {
    const label = node('label',text); label.append(control); section.append(label);
  }
  const sourceChoices = node('fieldset'); sourceChoices.className = 'aggregation-sources'; sourceChoices.append(node('legend','本次资料来源'));
  for (const [kind,name] of Object.entries(names)) {
    const label = node('label',name), check = node('input'); check.type = 'checkbox'; check.checked = true;
    choices.set(kind,check); label.prepend(check); sourceChoices.append(label);
  }
  section.append(sourceChoices,node('p','未选择的来源不会读取；全不选来源仍可提交，将记录无动作。关键词仅用于所选记忆来源。'));
  const send = node('button','生成聚合草稿'), refresh = node('button','刷新会话');
  const actions = node('div'); actions.className = 'behaviour-actions'; actions.append(send,refresh);
  const draftState = node('p'); draftState.className = 'behaviour-draft';
  const sessionStatus = node('p'); sessionStatus.setAttribute('role','status');
  const blocked = node('p'); blocked.className = 'muted';
  const status = node('p','尚未提交聚合请求。'); status.setAttribute('role','status');
  const identity = node('div'); identity.className = 'aggregation-identity';
  const result = node('div'); result.className = 'aggregation-result';
  const links = node('div'); links.className = 'behaviour-actions';
  const locationStatus = node('p'); locationStatus.setAttribute('role','status');
  const retry = node('button','重新核对本次运行'); retry.hidden = true;
  section.append(actions,draftState,sessionStatus,blocked,status,identity,result,links,locationStatus,retry); root.append(section);
  for (const control of [goal,keywords,...choices.values()]) drafts.track(control);
  let closed = false, pending = false, uncertain = false, awaiting = false;
  let generation = 0, readSequence = 0, sessionRequest = 0;
  let keywordsEdited = false, targetEdited = false, initialized = false;
  let rendered = '', run = '', selectedSession = /** @type {string|null} */ (null);
  /** @type {string|null} */ let baselineSession = null;
  let recording = '', observedInstance = runtime().instance;
  /** @type {Wire|null} */ let submitted = null;
  /** Only successor edits are unsubmitted while admission is pending or unknown.
   * @type {Map<HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement,string>|null} */ let submittedInputs = null;
  /** @type {AbortController|null} */ let observation = null;
  /** @type {AbortController|null} */ let sessionController = null;
  /** @type {ReturnType<typeof setTimeout>|null} */ let timer = null;
  /** @type {(()=>void)|undefined} */ let clearFacts;
  /** @type {HTMLButtonElement|null} */ let locate = null;
  function valid() {return !closed && section.isConnected;}
  function dirty() {
    if (submittedInputs && submitted) return selectedSession !== submitted.session_id
      || [...submittedInputs].some(([input,value]) => drafts.value(input) !== value);
    return drafts.dirty() || selectedSession !== baselineSession;
  }
  function cancelObservation() {readSequence++;observation?.abort();observation = null;}
  function clearResult() {clearFacts?.();clearFacts = undefined;rendered = '';result.replaceChildren();}
  function updateControls() {
    if (!valid()) return;
    const state = runtime();
    const reason = pending ? '提交中；当前编辑仅用于下一次请求。'
      : uncertain ? '准入未确认；先核对已有运行，不自动重投。'
      : !state.connected ? '连接未同步；可保留表单，恢复连接后再生成。'
      : state.unavailable ? '记录服务不可用；先处理未保存状态。'
      : state.active ? '已有运行或保存尚未收尾；不会排队。'
      : awaiting ? '正在核对已接受的原运行；不会重复提交。' : '';
    send.disabled = Boolean(reason); blocked.textContent = reason;
    draftState.textContent = dirty() ? '表单有未提交输入；更改不会影响已提交的请求。' : '表单没有新增的未提交修改。';
    draftState.dataset.dirty = String(dirty());
    if (locate && submitted) locate.textContent = state.session === submitted.session_id ? '在主对话查看草稿' : '切换到此会话并查看草稿';
  }
  for (const control of [target,goal,keywords,...choices.values()]) control.addEventListener('change',updateControls);
  keywords.addEventListener('input',() => {keywordsEdited = true;updateControls();});
  goal.addEventListener('input',() => {if (!keywordsEdited) keywords.value = goal.value;updateControls();});
  target.addEventListener('change',() => {targetEdited = true;selectedSession = target.selectedIndex > 0 ? target.value : null;updateControls();});
  async function sessions() {
    sessionController?.abort(); sessionController = new AbortController();
    const request = ++sessionRequest, process = runtime().instance;
    sessionStatus.textContent = '正在读取会话列表…';
    try {
      const response = await fetch('/api/sessions?limit=100',{signal:sessionController.signal});
      const body = await response.json();
      if (!valid() || request !== sessionRequest || process !== runtime().instance) return;
      if (!response.ok || !Array.isArray(body.sessions)) throw new Error();
      // Read the latest explicit choice when the response arrives, including
      // an intentionally cleared choice. Empty Session IDs remain valid IDs.
      if (!initialized && !targetEdited) selectedSession = typeof runtime().session === 'string' ? runtime().session : null;
      const selected = selectedSession, oldLabel = target.selectedOptions[0]?.textContent || selected;
      const placeholder = node('option','请选择目标会话');
      // The placeholder must not share a value with a legitimate empty ID.
      do {placeholder.value = crypto.randomUUID();} while (body.sessions.some((/** @type {Wire} */ item)=>item.session_id === placeholder.value) || selected === placeholder.value);
      target.replaceChildren(placeholder);
      for (const item of body.sessions) {
        const option = node('option',item.title || item.session_id || '空 Session ID'); option.value = item.session_id; target.append(option);
      }
      const option = selected === null ? null : [...target.options].slice(1).find(item => item.value === selected);
      if (option) option.selected = true;
      else if (selected !== null) {
        const retained = node('option',`${oldLabel} · 原选择，当前列表未包含`); retained.value = selected; target.append(retained); retained.selected = true;
      }
      if (!targetEdited) baselineSession = selectedSession;
      initialized = true;
      sessionStatus.textContent = `本次读取 ${body.sessions.length} 个会话${body.next_cursor ? '；还有未读取会话' : ''}。刷新列表不会改变已提交目标。`;
      updateControls();
    } catch {
      if (valid() && request === sessionRequest) sessionStatus.textContent = '会话读取失败，请刷新；原选择已保留。';
    }
  }
  /** @param {Wire} facts */
  function renderFacts(facts) {
    const key = JSON.stringify(facts);
    if (key === rendered) return;
    clearResult(); rendered = key;
    // This return is optional for existing callers until their owner retires.
    clearFacts = /** @type {any} */ (aggregationFacts(result,facts,sync));
    for (const [kind,source] of Object.entries(/** @type {Record<string,Wire>} */ (facts.sources || {}))) {
      if (Number.isSafeInteger(source.prepared_count)) result.append(node('p',`${names[kind] || kind}发送状态：已准备 ${source.prepared_count}；${source.dispatch_state === 'sent' ? '已发送' : source.dispatch_state === 'not_sent' ? '未发送' : '发送状态未知'}。准备不代表实际提供。`));
    }
  }
  function renderLinks() {
    if (!submitted || !run) return;
    links.replaceChildren(); locate = null;
    const runLink = node('a','查看同一运行与资料'); runLink.dataset.runLink = run; runLink.href = `/runs/${encodeURIComponent(run)}`;
    const sessionLink = node('a','只读查看原目标会话'); sessionLink.href = '/inbox?' + new URLSearchParams({session_id:submitted.session_id,view:'messages'});
    links.append(runLink,sessionLink);
  }
  /** @param {Wire} facts @param {string|null} saved @param {boolean} [identifiedProjection] */
  function acceptFacts(facts,saved,identifiedProjection=false) {
    // Host projections deliberately omit request. Their outer identity has
    // already matched this submitted Run and Session; durable facts retain it.
    if (!submitted || (!identifiedProjection && facts.request?.session_id !== submitted.session_id)) throw new Error('identity');
    renderFacts(facts);
    if (recording !== 'recorded') recording = saved || 'unknown';
    if (recording === 'recorded' || recording === 'failed') awaiting = false;
    if (recording === 'failed') status.textContent = facts.reply_disposition === 'reply' ? '草稿未保存，请查看运行记录状态。' : '本次聚合记录未保存；业务结果与保存失败分别核对。';
    else if (recording === 'pending') status.textContent = '正在保存…';
    else if (facts.error) status.textContent = `聚合失败：${facts.error}；未生成合格草稿。`;
    else if (facts.graph_result === 'NoAction') status.textContent = `本次聚合无动作：${reasons[facts.reason_code] || facts.reason_code || '原因未知'}；没有生成草稿。`;
    else if (facts.reply_disposition === 'reply') status.textContent = recording === 'recorded' ? '本次聚合已结束；草稿请在目标会话查看。' : '已生成合格草稿；记录状态未知，请核对原运行。';
    else status.textContent = '本次聚合结果尚无法确认，请核对原运行。';
    if (facts.reply_disposition === 'reply' && !locate) {
      locate = node('button'); const context = {run,session:submitted.session_id,generation};
      locate.addEventListener('click',async () => {
        if (!valid() || context.generation !== generation) return;
        const result = await dashboard.locateReply({process_instance_id:runtime().instance,session_id:context.session,run_id:context.run,action_id:crypto.randomUUID()});
        if (!valid() || context.generation !== generation) return;
        locationStatus.textContent = result.status === 'applied' ? '' : `草稿定位未完成：${result.reason || result.status}；表单已保留。`;
      });
      links.append(locate);
    }
    retry.hidden = false; updateControls();
  }
  async function observe() {
    if (!valid() || pending || uncertain || !run || !submitted || !runtime().connected) return;
    cancelObservation(); const request = readSequence, owned = generation, expectedRun = run, process = runtime().instance;
    const projection = runtime().projection;
    if (projection?.process_instance_id === process && projection.run_id === run && projection.session_id === submitted.session_id && projection.aggregation) {
      try {acceptFacts(projection.aggregation,projection.recording_state,true);} catch {status.textContent = '本次聚合状态身份无法核验，请查看原运行。';}
      return;
    }
    observation = new AbortController();
    try {
      // A retained trace may still describe the pre-restart computation. The
      // admitted Run's durable lifecycle decides whether that result survived.
      const locationResponse = await fetch('/api/runs/locate/' + encodeURIComponent(expectedRun) + '?' + new URLSearchParams({limit:'1',filter:'chat',process_instance_id:process}),{signal:observation.signal});
      const location = await locationResponse.json();
      if (!valid() || request !== readSequence || owned !== generation || process !== runtime().instance || !runtime().connected || uncertain) return;
      const summary = [...(location.runs || []),location.non_terminal].find(item=>item?.run_id === expectedRun);
      if (!locationResponse.ok || location.process_instance_id !== process || location.target?.run_id !== expectedRun || summary?.session_id !== submitted.session_id || summary?.purpose !== 'aggregation') throw new Error();
      if (summary.phase !== 'finished') {status.textContent = '正在读取资料或起草…';return;}
      if (summary.outcome === 'interrupted') {
        clearResult();renderLinks();awaiting = false;recording = summary.recording_state || 'unknown';
        status.textContent = '本次运行已中断（interrupted）；重启后无法证明原终态，未保存草稿不作为当前结果。';retry.hidden = false;return;
      }
      const response = await fetch('/api/run-evidence?' + new URLSearchParams({run_id:expectedRun}),{signal:observation.signal});
      const evidence = await response.json();
      if (!valid() || request !== readSequence || owned !== generation || process !== runtime().instance || !runtime().connected || uncertain) return;
      if (!response.ok || evidence.run_id !== expectedRun) throw new Error();
      const facts = evidence.memory?.aggregation;
      if (facts) acceptFacts(facts,evidence.recording_state);
      else {
        clearResult();renderLinks();awaiting = false;
        status.textContent = `本次运行已结束（${summary.outcome || '结局未知'}）；聚合资料证据无法确认，请查看原运行。不会重新生成。`;
        retry.hidden = false;
      }
    } catch {
      if (valid() && request === readSequence && owned === generation) {
        status.textContent = '本次运行读取失败；原 Run 与表单已保留，请重新核对。'; retry.hidden = false;
      }
    } finally {updateControls();}
  }
  async function update() {
    if (!valid()) return;
    const state = runtime();
    const instanceChanged = state.instance !== observedInstance;
    if (state.instance !== observedInstance || !state.connected) {
      cancelObservation(); observedInstance = state.instance;
      if (instanceChanged) void sessions();
      if (run && !uncertain && !pending) {status.textContent = '连接或服务实例已变化，请重新核对本次运行。';retry.hidden = false;}
    } else if (awaiting) await observe();
    updateControls();
    if (valid()) timer = setTimeout(() => void update(),700);
  }
  send.addEventListener('click',async () => {
    if (pending || uncertain || awaiting || send.disabled) return;
    if (target.selectedIndex <= 0) {status.textContent = '未提交：请选择目标会话；表单已保留。';return;}
    const body = {purpose:'aggregation',session_id:target.value,message:goal.value,keywords:keywords.value,sources:[...choices].filter(([,value]) => value.checked).map(([key]) => key)};
    const inputs = new Map([...drafts.inputs.keys()].map(input => [input,drafts.value(input)]));
    const owned = ++generation, process = runtime().instance;
    cancelObservation(); clearResult(); links.replaceChildren(); identity.replaceChildren(); locationStatus.textContent = '';locate = null;run = '';recording = '';retry.hidden = true;
    pending = true; status.textContent = '正在提交…'; submitted = body;submittedInputs = inputs;updateControls();
    identity.append(node('p',`本次目标 Session：${body.session_id}`),node('p',`提交的目标：${body.message}`),node('p',`关键词：${body.keywords}；所选来源：${body.sources.map(key => names[key]).join('、') || '全部未选'}`));
    try {
      const response = await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json','x-agent-alfred-csrf':csrf()},body:JSON.stringify(body)});
      const value = await response.json();
      if (!valid() || owned !== generation) return;
      if (process !== runtime().instance) throw new Error();
      if (!response.ok) {submittedInputs = null;status.textContent = `未提交：${value.code}；表单已保留。`;return;}
      if (response.status !== 202 || typeof value.run_id !== 'string' || value.session_id !== body.session_id) throw new Error();
      for (const [input,value] of inputs) drafts.saved(input,value);
      baselineSession = body.session_id;
      submittedInputs = null;
      run = value.run_id; awaiting = true; status.textContent = '正在读取资料或起草…';identity.append(node('p',`本次 Run：${run}`));renderLinks();
    } catch {
      if (valid() && owned === generation) {
        uncertain = true; status.textContent = '准入未确认；请查看已有运行，确认后刷新页面。不会自动重投。';
        const link = node('a','查看已有运行');link.href = '/runs';links.append(link);
      }
    } finally {pending = false;updateControls();}
  });
  refresh.addEventListener('click',() => void sessions());
  retry.addEventListener('click',() => void observe());
  void sessions();void update();
  return Object.assign(section,{
    getLeaveState:() => ({dirty:dirty(),summary:'手动聚合表单有未提交输入。',pending:pending || awaiting || uncertain}),
    close() {closed = true;generation++;sessionRequest++;sessionController?.abort();cancelObservation();if (timer) clearTimeout(timer);clearFacts?.();},
  });
}
