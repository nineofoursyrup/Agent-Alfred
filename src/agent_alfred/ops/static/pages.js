import {Drafts} from "./shell.js";
import {topologyView} from "./topology.js";
import {routingStatistics} from "./routing-statistics.js";
import {aggregationForm} from "./aggregation.js";
import { node, textBlocks } from "./dom.js";
import { outcomeLabel } from "./runs.js";
/** @typedef {Record<string, any>} Wire */

/** Every paginator keeps its own opaque cursor and single in-flight request. */
export class Pager {
  /** @param {HTMLElement} root @param {string} path @param {Record<string,string>} params @param {(body:Wire)=>void} append @param {string} label */
  constructor(root, path, params, append, label) {
    this.root = root;
    this.path = path;
    this.params = params;
    this.append = append;
    this.controller=new AbortController();
    this.cursor = "";
    this.busy = false;
    this.finished = false;
    this.button = node("button", label);
    this.label = label;
    this.button.addEventListener("click", () => void this.load());
    root.append(this.button);
  }
  close(){this.controller.abort();}
  async load() {
    if (this.busy || this.finished) return;
    this.busy = true;
    this.button.disabled = true;
    try {
      const params = {
        ...this.params,
        ...(this.cursor ? { cursor: this.cursor } : {}),
      };
      const response = await fetch(
        this.path + "?" + new URLSearchParams(params),{signal:this.controller.signal},
      );
      const body = await response.json();
      if (!this.root.isConnected) return;
      if (!response.ok) throw new Error("读取不可用");
      this.append(body);
      this.cursor = body.next_cursor || "";
      this.finished = !body.next_cursor;
      this.button.hidden = this.finished;
      this.button.textContent = this.label;
    } catch {
      if (this.root.isConnected)
        this.button.textContent = "读取暂不可用，点击重试";
    } finally {
      this.busy = false;
      this.button.disabled = false;
    }
  }
}

/** @param {HTMLElement} root @param {(id:string)=>void} resume */
export function inbox(root, resume) {
  const groups = node("div");
  let closePreview=()=>{};
  const preview = node("section");
  preview.setAttribute("aria-label", "会话只读预览");
  root.append(groups, preview);
  const seen = new Set();
  const pager = new Pager(
    root,
    "/api/sessions",
    { limit: "25" },
    (body) => {
      const sessions = [
        ...(body.non_terminal ? [body.non_terminal] : []),
        ...body.sessions,
      ];
      if (!sessions.length && !seen.size)
        groups.append(node("p", "还没有会话。点击下方「新建会话」开始。"));
      for (const session of sessions) {
        if (seen.has(session.session_id)) continue;
        seen.add(session.session_id);
        const card = node("article");
        card.className = "card";
        const open = node("button", session.title);
        card.append(open, node("p", session.created_at));
        groups.append(card);
        open.addEventListener("click", () => {closePreview();closePreview=showSession(preview, session, resume);});
      }
    },
    "更多会话",
  );
  void pager.load();
  return {close(){pager.close();closePreview();}};
}

/** @param {HTMLElement} root @param {Wire} session @param {(id:string)=>void} resume */
function showSession(root, session, resume) {
  const content = node("div");
  root.replaceChildren(content);
  const continueButton = node("button", "继续此会话");
  continueButton.addEventListener("click", () => resume(session.session_id));
  content.append(
    node("h2", session.title),
    node("p", "只读预览"),
    continueButton,
  );
  const messages = node("div");
  const runs = node("div");
  content.append(messages, runs);
  const messagePager = new Pager(
    content,
    "/api/sessions/messages",
    { session_id: session.session_id, page_size: "25" },
    (body) => {
      for (const message of body.messages)
        messages.append(node("p", textBlocks(message.blocks)));
    },
    "更多会话消息",
  );
  const seen = new Set();
  const runPager = new Pager(
    content,
    "/api/sessions/runs",
    { session_id: session.session_id, limit: "25" },
    (body) => {
      for (const run of body.runs) {
        if (seen.has(run.run_id)) continue;
        seen.add(run.run_id);
        const row = node("article");
        row.className = "card";
        row.append(
          node(
            "p",
            `${run.gateway === "cli" ? "CLI" : run.gateway === "web" ? "Web" : run.gateway} · ${run.accepted_at}`,
          ),
        );
        row.append(node("p", outcomeLabel(run)));
        if (run.reply_preview !== null)
          row.append(node("p", run.reply_preview));
        const link = node("a", "查看运行");
        link.href = `/runs/${encodeURIComponent(run.run_id)}?filter=chat`;
        row.append(link);
        runs.append(row);
      }
    },
    "更多会话运行",
  );
  void messagePager.load();
  void runPager.load();
  return ()=>{messagePager.close();runPager.close();};
}

export {modelsPage} from "./models.js";
export {connectionsPage} from "./connections.js";

/** @param {HTMLElement} root @param {()=>string} csrf @param {()=>Wire} runtime @param {import("./memory.js").MemorySync} memory @param {()=>string} instance */
export function behaviourPage(root, csrf, runtime, memory, instance) {
  const drafts=new Drafts();
  let alive=true;
  const routing = node("section"); routing.setAttribute("aria-label", "消息分流");
  routing.append(node("h2", "消息分流"), node("p", "持续设置 · CLI/Web 共享。默认关闭；保存后下一 Run 生效。回复进入当前会话，静默时只记用户消息，不生成助手消息或长期记忆。"));
  root.append(routing);
  const aggregation = aggregationForm(root, csrf, runtime, memory);
  let state = /** @type {Wire} */ ({});
  const label = node('label', '启用消息分流');
  const enabled = document.createElement('input');
  enabled.type = 'checkbox';
  enabled.disabled = true;
  drafts.track(enabled);
  label.prepend(enabled);
  const notice = node('p'); notice.setAttribute('role', 'status');
  const save = node('button', '保存设置'); save.disabled = true;
  const refresh = node('button', '刷新设置');
  const recover = node('button', '备份原文件并恢复为关闭'); recover.hidden = true;
  const actual = node('p'); actual.setAttribute('role', 'status');
  routing.append(actual, node('p', '默认关闭。启用后识别纯问候、致谢及明确无需回复的消息；实际任务仍进入完整回答。'),
    label, save, refresh, recover, notice);
  const views = [topologyView(routing, "message_routing", runtime, memory), topologyView(aggregation, "manual_aggregation", runtime, memory)];

  const statistics = routingStatistics(routing, instance);
  async function read() {
    try {
      const response = await fetch('/api/behaviour');
      if (!response.ok) throw new Error('读取失败');
      state = await response.json();
      if (!alive || !root.isConnected) return;
      actual.textContent = state.status === 'ok' ? `已保存的分流设置：${state.enabled ? '开启' : '关闭'}。能查看结构不代表已经启用或可以成功执行。` : `分流设置未知 / ${state.status}。`;
      if(!drafts.dirty()){enabled.checked=state.enabled;drafts.saved(enabled);}
      enabled.disabled = state.status !== 'ok';
      save.disabled = state.status !== 'ok';
      recover.hidden = state.status === 'ok' || !state.fingerprint;
      notice.textContent = state.status === 'ok' ? '设置在下一 Run 生效。'
        : `配置不可用（${state.status}）；普通聊天仍可使用。恢复会先备份原文件。`;
    } catch { notice.textContent = '设置读取失败，请刷新。'; }
  }
  /** @param {string} action */
  async function write(action) {
    const submitted=enabled.checked;
    save.disabled = true; recover.disabled = true;
    try {
      const response = await fetch('/api/behaviour', {
        method:'POST', headers:{'Content-Type':'application/json', 'x-agent-alfred-csrf':csrf()},
        body:JSON.stringify({action, expected_revision:state.revision,
          enabled:enabled.checked, fingerprint:state.fingerprint}),
      });
      const result = await response.json();
      if (!alive || !root.isConnected) return;
      if (!response.ok) {
        notice.textContent = `未保存（${result.code}${result.cause ? ' / ' + result.cause : ''}）。选择已保留，请刷新后重试。${result.backup_path ? '备份：' + result.backup_path : ''}`;
        return;
      }
      state = result; if(enabled.checked===submitted)enabled.checked=result.enabled;drafts.saved(enabled,String(result.enabled));
      actual.textContent = `已保存的分流设置：${state.enabled ? '开启' : '关闭'}。能查看结构不代表已经启用或可以成功执行。`;
      enabled.disabled = false; recover.hidden = true;
      notice.textContent = '已保存；下一 Run 生效。';
    } catch { notice.textContent = '保存结果未确认，请刷新核验。'; }
    finally { save.disabled = state.status !== 'ok'; recover.disabled = false; }
  }
  save.addEventListener('click', () => void write('save'));
  recover.addEventListener('click', () => void write('recover'));
  refresh.addEventListener('click', () => void read());
  void read();
  return {
    getLeaveState(){return {dirty:drafts.dirty()||aggregation.getLeaveState().dirty,summary:"分流设置或聚合表单有未提交输入。"};},
    sync() {statistics.sync();},
    disconnect() {statistics.disconnect();},
    close() {alive=false;statistics.close(); for (const view of views) view.close();},
  };
}
