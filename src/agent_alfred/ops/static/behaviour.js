import {dashboard} from './app.js';
import {Drafts} from './shell.js';
import {node} from './dom.js';
import {topologyView} from './topology.js';
import {routingStatistics} from './routing-statistics.js';
import {aggregationForm} from './aggregation.js';

/** @typedef {Record<string, any>} Wire */
const settingsReasons = /** @type {Record<string,string>} */ ({
  settings_unreadable:'配置文件不可读',
  settings_invalid:'配置文件损坏',
  settings_schema_newer:'配置格式不支持',
  settings_write_unconfirmed:'上次写入未确认',
});
/** @param {Wire} state */
function validSettings(state) {
  return state?.schema_version === 1 && Number.isSafeInteger(state.revision) && state.revision >= 0
    && typeof state.enabled === 'boolean'
    && (state.status === 'ok' || Object.hasOwn(settingsReasons,state.status));
}

/** Behaviour owns each draft and observation; the shell owns navigation and Host facts.
 * @param {HTMLElement} root @param {()=>string} csrf @param {()=>Wire} runtime
 * @param {import('./memory.js').MemorySync} memory @param {()=>string} instance
 */
export function behaviourPage(root, csrf, runtime, memory, instance) {
  root.classList.add('behaviour-page');
  const drafts = new Drafts();
  let alive = true, pending = false, readSequence = 0;
  /** @type {AbortController|null} */ let readController = null;
  /** @type {Wire|null} */ let current = null;
  /** @type {Wire|null} */ let baseline = null;
  /** Submitted input remains distinct from the confirmed CAS baseline, including a lost receipt.
   * @type {boolean|null} */ let submittedChoice = null;
  // subscribeState is the immutable Host source. MainBar's retained reply cache
  // must never nominate a result for this form.
  let host = {...dashboard.runtime(),projection:null};
  const unsubscribe = dashboard.subscribeState(state => {if (typeof state.instance === 'string') host = state;});
  const pageRuntime = () => host;
  const routing = node('section'); routing.className = 'card behaviour-routing';
  routing.setAttribute('aria-label','消息分流');
  routing.append(node('h2','消息分流'),node('p','持续设置 · CLI/Web 共享。默认关闭；保存后下一 Run 生效。回复进入当前会话，静默时只记用户消息，不生成助手消息或长期记忆。'));
  const actual = node('p'); actual.className = 'behaviour-saved';
  const choiceState = node('p'); choiceState.className = 'behaviour-draft';
  const reading = node('p'); reading.setAttribute('role','status');
  const receipt = node('p'); receipt.setAttribute('role','status'); receipt.className = 'behaviour-receipt';
  const label = node('label','启用消息分流');
  const enabled = node('input'); enabled.type = 'checkbox'; enabled.disabled = true;
  drafts.track(enabled); label.prepend(enabled);
  const save = node('button','保存设置');
  const refresh = node('button','刷新设置');
  const rebase = node('button','基于当前版本继续编辑'); rebase.hidden = true;
  const recover = node('button','备份原文件并恢复为关闭'); recover.hidden = true;
  const actions = node('div'); actions.className = 'behaviour-actions'; actions.append(save,refresh,rebase,recover);
  routing.append(actual,node('p','启用后识别纯问候、致谢及明确无需回复的消息；实际任务仍进入完整回答。'),label,choiceState,actions,reading,receipt);
  root.append(routing);
  const aggregation = aggregationForm(root,csrf,pageRuntime,memory);
  const views = [topologyView(routing,'message_routing',pageRuntime,memory),topologyView(aggregation,'manual_aggregation',pageRuntime,memory)];
  const statistics = routingStatistics(routing,instance);

  function cancelRead() {readSequence++; readController?.abort(); readController = null;}
  function dirty() {return submittedChoice === null ? drafts.dirty() : enabled.checked !== submittedChoice;}
  function present() {
    const changed = current && baseline && (current.revision !== baseline.revision || current.fingerprint !== baseline.fingerprint);
    actual.textContent = !current ? '已保存的分流设置：尚未核验。'
      : current.status === 'ok' ? `已保存的分流设置：${current.enabled ? '开启' : '关闭'} · revision ${current.revision}。能查看结构不代表已经启用或可以成功执行。`
      : `分流设置未知 / ${settingsReasons[current.status]}（${current.status}）。`;
    choiceState.textContent = baseline ? `${dirty() ? '未提交选择' : submittedChoice !== null ? '已提交选择' : '当前选择'}：${enabled.checked ? '开启' : '关闭'} · 编辑基线 revision ${baseline.revision}。${changed ? '已读取的配置已变化；原选择和基线保留，可比较后明确采用当前版本。' : ''}` : '正在核对设置。';
    choiceState.dataset.dirty = String(dirty());
    enabled.disabled = current?.status !== 'ok';
    save.disabled = pending || current?.status !== 'ok' || baseline?.status !== 'ok';
    refresh.disabled = pending;
    rebase.hidden = !changed || current?.status !== 'ok'; rebase.disabled = pending;
    recover.hidden = !current || current.status === 'ok' || !current.fingerprint;
    recover.disabled = pending;
  }
  async function read() {
    if (!alive || pending) return;
    cancelRead(); const request = readSequence;
    const expectedInstance = instance();
    readController = new AbortController(); reading.textContent = '正在核对当前配置…';
    try {
      const response = await fetch('/api/behaviour',{signal:readController.signal});
      const value = await response.json();
      if (!alive || request !== readSequence || expectedInstance !== instance()) return;
      if (!response.ok || !validSettings(value)) throw new Error();
      current = value;
      if (!baseline || (submittedChoice === null && !drafts.dirty())) {
        baseline = value; enabled.checked = value.enabled; drafts.saved(enabled);
      }
      reading.textContent = value.status === 'ok' ? '当前值已核对；设置在下一 Run 生效。'
        : `配置不可用：${settingsReasons[value.status]}（${value.status}）；普通聊天仍可使用。恢复会先备份原文件。`;
      present();
    } catch {
      if (alive && request === readSequence) reading.textContent = '设置读取失败；已显示的值仅为上次观察，选择已保留，请刷新。';
    }
  }
  /** @param {'save'|'recover'} action */
  async function write(action) {
    if (!alive || pending || !current || !baseline) return;
    const original = action === 'recover' ? current : baseline;
    const submitted = enabled.checked;
    const expectedInstance = instance();
    cancelRead(); submittedChoice = submitted; pending = true; receipt.textContent = action === 'recover' ? '正在备份原文件并恢复…' : '正在保存提交时的选择…'; present();
    try {
      const response = await fetch('/api/behaviour',{
        method:'POST',headers:{'Content-Type':'application/json','x-agent-alfred-csrf':csrf()},
        body:JSON.stringify({action,expected_revision:original.revision,enabled:submitted,fingerprint:original.fingerprint}),
      });
      const value = await response.json();
      if (!alive) return;
      if (expectedInstance !== instance()) {receipt.textContent = '保存结果未确认：服务实例已改变，请刷新核验。'; return;}
      if (!response.ok) {
        submittedChoice = null;
        const conflict = value.cause === 'external_change' ? '磁盘外部修改' : value.cause === 'stale_revision' ? '版本冲突' : settingsReasons[value.code] || value.code;
        receipt.textContent = `未保存（${value.code}${value.cause ? ' / '+value.cause : ''}）：${conflict}。选择已保留，请刷新核对。${value.backup_path ? '备份：'+value.backup_path : ''}`;
        return;
      }
      if (!validSettings(value) || value.status !== 'ok' || value.revision !== original.revision + 1 || value.enabled !== (action === 'recover' ? false : submitted)) throw new Error();
      current = baseline = value;
      if (enabled.checked === submitted) enabled.checked = value.enabled;
      drafts.saved(enabled,String(value.enabled));
      submittedChoice = null;
      reading.textContent = '';
      receipt.textContent = action === 'recover' ? '已备份原文件并恢复为关闭；下一 Run 生效。' : '已保存；下一 Run 生效。';
      if (action === 'recover' && value.backup_path) reading.textContent = `原文件备份：${value.backup_path}`;
    } catch {
      if (alive) receipt.textContent = '保存结果未确认，请刷新核验。不会自动重送；只读核对不代表取得原保存回执。';
    } finally {pending = false; if (alive) present();}
  }
  enabled.addEventListener('change',present);
  save.addEventListener('click',() => void write('save'));
  recover.addEventListener('click',() => void write('recover'));
  refresh.addEventListener('click',() => void read());
  rebase.addEventListener('click',() => {
    if (!current || current.status !== 'ok' || pending) return;
    baseline = current; drafts.saved(enabled,String(current.enabled)); submittedChoice = null; present();
  });
  present(); void read();
  return {
    getLeaveState() {const a = aggregation.getLeaveState(); return {dirty:dirty() || a.dirty,summary:'分流设置或聚合表单有未提交输入。',pending:pending || submittedChoice !== null || a.pending};},
    sync() {statistics.sync();},
    disconnect() {cancelRead();statistics.disconnect();},
    close() {alive = false;cancelRead();unsubscribe();statistics.close();aggregation.close();for (const view of views) view.close();},
  };
}
