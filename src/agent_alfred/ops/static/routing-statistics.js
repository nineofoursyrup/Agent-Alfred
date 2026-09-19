import {node} from './dom.js';
/** @typedef {Record<string, any>} Wire */
const ranges = {'24h':'24小时', '7d':'7天', '30d':'30天', all:'全部'};
const routes = {quick:'快速回复', full:'完整回答', fallback:'图内保守兜底', no_action:'不回复', context_failure:'上下文失败提示'};
/** @param {Wire} value @param {boolean} comparable */
function ratio(value, comparable) {
  if (!comparable) return "历史口径未知，仅列次数与缺失";
  return value.value === null ? `${value.numerator}/${value.denominator} · 无可计算样本`
    : `${value.numerator}/${value.denominator} · ${(value.value * 100).toLocaleString(undefined, {maximumFractionDigits:1})}%`;
}
/** @param {HTMLElement} root @param {()=>string} instance */
export function routingStatistics(root, instance) {
  const panel = document.createElement('details'); panel.className = 'card routing-statistics';
  panel.append(node('summary', '路由统计'));
  const label = node('label', '统计范围');
  const range = document.createElement('select'); range.setAttribute('aria-label', '统计范围');
  for (const [key, text] of Object.entries(ranges)) {
    const option = document.createElement('option'); option.value = key; option.textContent = text; range.append(option);
  }
  range.value = '7d'; label.append(range);
  const refresh = node('button', '刷新统计'); refresh.type = 'button';
  const status = node('p'); status.setAttribute('role', 'status'); status.setAttribute('aria-live','polite');
  const content = node('div');
  panel.append(node('p', '跨会话 CLI/Web 已准入聊天 Run；按准入时刻归窗，仅启用且终态已持久保存的 Run 进入主结果。未知不代表未发生。'),
    label, refresh, status, content);
  root.append(panel);
  let sequence = 0;
  let process = instance();
  /** @type {AbortController | null} */ let controller = null;
  function cancel() {sequence++; controller?.abort(); controller = null;}
  const observer = new MutationObserver(() => {
    if (!panel.isConnected) {cancel(); observer.disconnect();}
  });
  observer.observe(document.body, {childList:true, subtree:true});
  async function load() {
    cancel(); const request = sequence;
    const expectedProcess = instance();
    controller = new AbortController();
    const window = range.value;
    content.replaceChildren(); status.textContent = '正在读取统计…';
    try {
      const response = await fetch('/api/behaviour/routing-statistics?' + new URLSearchParams({window}), {signal:controller.signal});
      const data = await response.json();
      if (request !== sequence || !panel.open || !panel.isConnected) return;
      if (response.ok && (instance() !== expectedProcess || data.process_instance_id !== instance())) {
        invalidate('进程已改变，请刷新统计。'); return;
      }
      if (!response.ok) {
        const reason = data.code === 'query_timeout' ? '查询超过2秒工作预算，请缩小范围或重试'
          : data.code === 'statistics_invalid_data' ? '持久数据的样本归属无法判定，请修复数据后重试'
          : '统计读取不可用，请重试';
        throw new Error(reason);
      }
      status.textContent = '';
      render(content, data);
    } catch (error) {
      if (request === sequence && panel.open && panel.isConnected)
        status.textContent = `读取失败：${error instanceof Error ? error.message : '请重试'}。`;
    }
  }
  /** @param {string} message */
  function invalidate(message) {cancel(); content.replaceChildren(); if (panel.open) status.textContent = message;}
  panel.addEventListener('toggle', () => {if (panel.open) void load(); else {cancel(); content.replaceChildren();}});
  range.addEventListener('change', () => {if (panel.open) void load();});
  refresh.addEventListener('click', () => {if (panel.open) void load();});
  return {
    sync() {if (instance() !== process) {process = instance(); invalidate('进程已改变，请刷新统计。');}},
    disconnect() {invalidate('连接已中断，请刷新统计。');},
    close() {cancel(); observer.disconnect();},
  };
}
/** @param {HTMLElement} root @param {Wire} data */
function render(root, data) {
  const sample = data.sample;
  const local = (/** @type {string} */ v) => new Date(v).toLocaleString();
  root.append(node('p', `范围：${ranges[/** @type {keyof typeof ranges} */ (data.window)]} · ${data.from_at ? local(data.from_at) : '无下界'}（含）— ${local(data.as_of)}（不含）`),
    node('p', `显示时区：${Intl.DateTimeFormat().resolvedOptions().timeZone}；判窗 UTC。快照时间：${data.as_of}；读取身份：${data.read_id}；进程：${data.process_instance_id}`),
    node('p', `已准入 ${sample.admitted} · 启用 ${sample.enabled} · 关闭 ${sample.disabled} · 启用未知 ${sample.unknown}`),
    node('p', `启用样本：已持久终态 ${sample.finished} · 尚未记录终态 ${sample.pending}；启用状态未知的已知图前绕行 ${sample.unknown_bypass}`));
  if (!sample.admitted) root.append(node('p', '此范围内没有已准入的聊天 Run。'));
  else if (!sample.enabled) root.append(node('p', '此范围内没有可证明已启用的 Run。'));
  else if (!sample.finished) root.append(node('p', '有启用 Run，但尚无已持久保存的终态；无可计算样本。'));
  root.append(node('p', '路由决议不代表成功。组内可能包含不同模型、工具和参数；这些数字不表示分类准确率、成功率、费用节省或因果效果。'));
  for (const group of data.groups) {
    const section = node('section');
    section.append(node('h3', group.current ? '当前版本' : group.comparable ? '其他已知版本' : '历史／版本未知'));
    if (group.comparable) section.append(node('p', `统计口径：${group.metrics_version}；路由策略：${group.policy_version}`));
    const d = group.decisions;
    section.append(node('p', `终态 ${group.total} · 已知决议 ${d.known} · 明确未形成决议 ${d.none} · 未知决议 ${d.unknown}；决议覆盖 ${ratio(d.coverage, group.comparable)}`));
    const table = node('table'); const head = node('tr');
    for (const text of ['路由决议', '次数', '分支比例（分母为已知决议）']) head.append(node('th', text));
    table.append(head);
    for (const [key, label] of Object.entries(routes)) {
      const row = node('tr'); row.append(node('th', label), node('td', String(d.counts[key])), node('td', ratio(d.ratios[key], group.comparable))); table.append(row);
    }
    section.append(table);
    for (const [key, name] of [['fallback','图后故障回退'], ['recovered','上下文恢复']]) {
      const m = group[key];
      section.append(node('p', `${name}：发生 ${m.yes} · 已知未发生 ${m.no} · 未知 ${m.unknown}；发生率 ${ratio(m.rate, group.comparable)}；覆盖 ${ratio(m.coverage, group.comparable)}`));
    }
    section.append(node('p', `图前绕行：${group.bypass.yes} · 未知 ${group.bypass.unknown}`), node('p', `被阻止的回退：${group.blocked.total}`));
    for (const [reason, count] of Object.entries(group.blocked.reasons)) section.append(node('p', `${reason}：${count}`));
    const missingLabels = /** @type {Record<string,string>} */ ({summary_missing:'缺少已持久保存的结果摘要', unsupported_format:'不支持的摘要格式', unsupported_semantics:'不支持的语义版本', legacy_incomplete:'旧格式证据不完整', invalid_or_missing_metric:'指标缺失、无效或矛盾'});
    for (const [reason, count] of Object.entries(group.missing_reasons || {})) section.append(node('p', `缺失原因：${missingLabels[reason] || '证据未知'} · ${count} Run`));
    root.append(section);
  }
}
