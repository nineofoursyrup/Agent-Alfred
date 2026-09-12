import { node } from './dom.js';
/** @typedef {Record<string, any>} Wire */

/** @param {string} path @param {Wire|undefined} body @param {string} csrf @param {AbortSignal} [signal] */
export async function request(path, body, csrf, signal) {
  const response = await fetch(path, {method: body ? 'POST' : 'GET', signal,
    cache: 'no-store', headers: body ? {'Content-Type': 'application/json', 'x-agent-alfred-csrf': csrf} : {},
    ...(body ? {body: JSON.stringify(body)} : {})});
  const value = await response.json();
  return {ok: response.ok, value, status: response.status};
}
/** @param {HTMLElement} root @param {()=>string} csrf */
export function toolsPage(root, csrf) {
  const state = node('p', '读取工具目录…');
  const list = node('section');
  const refresh = node('button', '核对当前授权');
  const reapply = node('button', '重新应用已保存授权');
  root.append(state, refresh, reapply, node('p', '从 MainBar 提出工具请求；本页查看能力和授权。'), list);
  let revision = 0, generation = 0, alive = true, busy = false, online = true;
  /** @type {Map<string,{value:string, revision:number}>} */ const drafts = new Map();
  /** @type {Wire|null} */ let current = null;
  const controller = new AbortController();
  /** @param {Wire} value */
  function render(value) {
    current = value; revision = value.revision;
    state.textContent = `配置：${value.configuration_state} · 保存版本 ${revision} · 生效：${value.application_state}${value.external_change ? ' · 检测到磁盘变化，禁止覆盖' : ''}`;
    if (value.active_policy_retained) state.append(node('span', ' · 在途操作保留原政策；当前外部授权待核验，空闲后暂停外部能力。'));
    reapply.disabled = busy || value.configuration_state !== 'ok';
    list.replaceChildren();
    for (const tool of value.tools) {
      const card = node('article'); card.className = 'card';
      card.append(node('h2', tool.name), node('p', tool.description),
        node('p', `来源 ${tool.source_id} · 能力 ${tool.capability_id} · ${tool.effect}`),
        node('p', `可用性：${tool.availability} · 模型暴露：${tool.exposure} · ${tool.reason || '可使用'}`),
        node('p', tool.effect === 'external' ? `外部连接：${tool.connection === 'unverified' ? '尚未验证' : tool.connection}` : '本地工具，不需要外部授权'));
      const history = node('a', '查看包含该工具的运行');
      history.href = '/ops?tool=' + encodeURIComponent(tool.identity);
      card.append(history);
      if (tool.effect === 'external') {
        const select = node('select'); select.setAttribute('aria-label', `${tool.name} 授权草稿`);
        for (const [key, label] of [['unset', '未决定'], ['allowed', '允许'], ['denied', '拒绝']]) {
          const option = node('option', label); option.value = key; select.append(option);
        }
        select.value = drafts.get(tool.identity)?.value || value.authorizations[tool.identity] || 'unset';
        select.disabled = busy || !online;
        select.onchange = () => drafts.set(tool.identity, {value:select.value, revision:drafts.get(tool.identity)?.revision ?? revision});
        const save = node('button', '保存授权'); save.disabled = busy || value.configuration_state !== 'ok';
        card.append(node('p', `服务端当前保存值：${value.authorizations[tool.identity] || 'unset'}`), select, save);
        if (value.disk_configuration) card.append(node('p', `磁盘外部版本 ${value.disk_configuration.revision}：${value.disk_configuration.authorizations[tool.identity] || 'unset'}；保留当前运行时配置，请核对文件并重启加载。`));
        save.onclick = () => { if (!drafts.has(tool.identity)) drafts.set(tool.identity, {value:select.value,revision}); void mutate('/api/tools/authorization',
          {identity: tool.identity, authorization: select.value, expected_revision: drafts.get(tool.identity)?.revision ?? revision}); };
        const draft = drafts.get(tool.identity);
        if (draft && draft.revision !== revision) {
          card.append(node('p', `草稿基于版本 ${draft.revision}；当前版本 ${revision}，请比较后确认。`));
          const accept = node('button', '基于当前版本继续编辑'); accept.onclick = () => {drafts.set(tool.identity, {value:select.value,revision}); render(value);}; card.append(accept);
        }
      }
      list.append(card);
    }
  }
  async function load() {
    if (!online) return;
    const mine = ++generation;
    try {
      const result = await request('/api/tools', undefined, csrf(), controller.signal);
      if (!alive || mine !== generation) return;
      if (!result.ok || !Array.isArray(result.value.tools)) throw new Error();
      render(result.value);
      return result.value;
    } catch { if (alive && mine === generation) state.textContent = '当前授权状态无法核实；草稿保留。'; }
  }
  /** @param {string} path @param {Wire} body */
  async function mutate(path, body) {
    if (busy || !online) return;
    busy = true; ++generation;
    const commandGeneration = generation;
    if (current) render(current);
    let notice = '提交结果尚未确认，仅核对当前值；不会自动重送。';
    /** @type {Wire|null} */ let receipt = null;
    try {
      const result = await request(path, body, csrf(), controller.signal);
      if (!alive || !online || commandGeneration !== generation) return;
      if (result.ok && Array.isArray(result.value.tools)) {
        receipt = result.value;
        notice = `服务端收到版本 ${receipt?.revision} 操作回执；当前状态以本次回读为准。`;
      } else if (result.value.error || result.value.code) notice = `保存未完成：${result.value.error?.code || result.value.code}。草稿保留，请比较当前值。`;
    } catch { /* Receipt loss is not permission to repeat the command. */ }
    finally {
      busy = false;
      if (alive && online) {
        const checked = await load();
        if (alive && online && checked) {
          if (receipt && checked.revision === receipt.revision && checked.application_state === receipt.application_state && !checked.external_change) {
            if (body.identity && drafts.get(body.identity)?.revision === body.expected_revision) drafts.delete(body.identity);
            render(checked);
            notice = checked.application_state === 'applied' ? '服务端确认操作，授权已生效。' : '已保存，尚未生效；请显式重新应用。';
          }
          state.append(node('span', ' ' + notice));
        }
      }
    }
  }
  refresh.onclick = () => void load();
  reapply.onclick = () => void mutate('/api/tools/reapply', {expected_revision: revision});
  const focus = () => { if (!busy) void load(); };
  window.addEventListener('focus', focus);
  void load();
  return {sync() {online = true; focus();}, disconnect() {online = false; ++generation; state.textContent = '离线，当前生效状态待核验；草稿保留。';},
    close() {alive = false; ++generation; controller.abort(); window.removeEventListener('focus', focus);}};
}
