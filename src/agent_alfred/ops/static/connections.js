import {node} from "./dom.js";
import {dashboard} from "./app.js";
/** @typedef {Record<string, any>} Wire */
const STATE_LABEL = /** @type {Record<string,string>} */ ({
  unconfigured: "未配置",
  configured_untested: "已配置未测试",
  connected: "已连接",
  error: "错误",
});
const CATALOG_LABEL = /** @type {Record<string,string>} */ ({
  unfetched: "未拉取",
  fresh: "新鲜",
  stale: "过期",
  unavailable: "不可用",
});

/** @param {HTMLElement} root @param {()=>string} csrf */
export function connectionsPage(root, csrf) {
  root.classList.add("settings-page");
  let alive=true, generation=0;
  const opened=new Map();
  const pending=new Set();
  let pendingMCP=false, pendingReread=false, connected=dashboard.runtime().connected;
  /** @type {Wire|null} */ let current=null;
  const reads=new AbortController();
  let revision = -1;
  let sequence = 0;
  let readSequence = 0;
  let noticeSequence = 0;
  let instance = "";
  const retired = new Set();
  const channel = new BroadcastChannel("alfred-integrations");
  const notice = node("p");
  const configurationNotice = node("p");
  root.append(configurationNotice, notice);
  channel.onmessage = () => void refresh();
  const focused = () => void refresh();
  window.addEventListener("focus", focused);
  const list = node("div");
  list.setAttribute("aria-label", "端点连接");
  const reread = node("button", "重新读取 .env");
  reread.addEventListener("click", () => void postReread());
  root.append(reread, list);

  async function postReread() {
    const token = csrf();
    if (!token || !connected || pendingReread) return;
    pendingReread=true;
    const epoch=generation;
    reread.disabled = true;
    const attempt = ++sequence;
    try {
      const response = await fetch("/api/connections/reread", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-agent-alfred-csrf": token,
        },
        body: "{}",
      });
      const body = await response.json();
      if(!alive || epoch!==generation)return;
      if (response.ok) acceptMutation(body, attempt);
      else mutationFailure(body.code || "error", attempt);
    } catch {
      if(epoch===generation)mutationFailure("重读结果未确认，请核对当前状态后重试。", attempt);
    } finally {
      if(epoch===generation){pendingReread=false;reread.disabled=!connected;}
    }
  }

  /** @param {string} endpointId @param {HTMLButtonElement} button */
  async function verifyCredentials(endpointId, button) {
    const token = csrf();
    if (!token || !connected) return;
    const epoch=generation;
    if (button.disabled || pending.has(endpointId)) return;
    pending.add(endpointId);
    button.disabled = true;
    const attempt = ++sequence;
    try {
      const response = await fetch("/api/connections/probe", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-agent-alfred-csrf": token,
        },
        body: JSON.stringify({ endpoint_id: endpointId }),
      });
      const body = await response.json();
      if(!alive || epoch!==generation)return;
      if (response.ok) acceptMutation(body, attempt);
      else mutationFailure(body.code || "error", attempt);
    } catch {
      if(epoch===generation)mutationFailure("测试结果未确认，请核对当前状态。", attempt);
    } finally {
      if(epoch===generation){pending.delete(endpointId);if(current)render(current);}
    }
  }

  /** @param {string} message @param {number} attempt */
  function mutationFailure(message, attempt) {
    if (attempt !== sequence || !list.isConnected) return;
    ++noticeSequence;
    notice.textContent = message;
  }

  /** @param {Wire} body @param {number} attempt */
  function acceptMutation(body, attempt) {
    if (attempt !== sequence && (body.process_instance_id || instance) === instance
        && (body.connections_revision ?? body.integration_revision ?? 0) <= revision) return;
    if (!render(body)) return;
    ++readSequence;
    if (attempt === sequence && body.integration_application !== "not_applied") {
      ++noticeSequence;
      notice.textContent = "";
    }
    channel.postMessage("changed");
  }

  /** @param {string} value */
  function adoptInstance(value) {
    if (retired.has(value)) return false;
    if (value && value !== instance) {
      if (instance) retired.add(instance);
      instance = value; revision = -1; generation++; pending.clear(); pendingMCP=false; pendingReread=false;current=null;
      ++sequence; ++noticeSequence;
      notice.textContent = "";
      configurationNotice.textContent = "";
    }

    return true;
  }

  /** @param {Wire} body */
  function render(body) {
    if (!alive || !list.isConnected || !adoptInstance(body.process_instance_id || instance)) return false;
    const incoming = body.connections_revision ?? body.integration_revision ?? 0;
    if (incoming < revision) return false;
    revision = incoming; current=body;
    reread.disabled=!connected || pendingReread;
    // This current configuration fact is independent of operation receipts.
    configurationNotice.textContent = body.integration_application === "not_applied"
      ? "配置未能一致生效，外部能力已暂停；请修复后重新读取 .env。" : "";
    const focused=document.activeElement;
    const focusKey=focused instanceof HTMLElement && list.contains(focused)?focused.dataset.focusKey:null;
    list.replaceChildren();
    const endpointGroup=node("section"); endpointGroup.className="settings-group";endpointGroup.setAttribute("aria-label","模型端点");
    endpointGroup.append(node("h2","模型端点"));
    const integrationGroup=node("section");integrationGroup.className="settings-group";integrationGroup.setAttribute("aria-label","可选集成");
    integrationGroup.append(node("h2","可选集成"));
    list.append(endpointGroup,integrationGroup);
    /** @param {string} key @param {string} label */
    function details(key,label) {
      const box=node("details"), toggle=node("summary",label);toggle.dataset.focusKey=key;
      box.open=opened.get(key) || false;box.append(toggle);
      box.addEventListener("toggle",()=>{opened.set(key,box.open);if(!box.open && box.contains(document.activeElement))toggle.focus({preventScroll:true});});
      return box;
    }
    for (const endpoint of body.endpoints || []) {
      const card=node("article");card.className="connection-row";card.dataset.endpoint=endpoint.endpoint_id;
      const observation=node("p",`连接：${STATE_LABEL[endpoint.observation.state] || endpoint.observation.state} ${endpoint.observation.reason || ""}`);
      observation.dataset.dimension="connection";
      if(endpoint.observation.checked_at) observation.append(document.createTextNode(` ${endpoint.observation.checked_at} ${endpoint.observation.checked_via || ""}`));
      const catalog=node("p",`目录：${CATALOG_LABEL[endpoint.catalog.health] || endpoint.catalog.health}；上次成功 ${endpoint.catalog.last_success_at || "尚无"} ${endpoint.catalog.last_error || ""} ${endpoint.catalog.retry_at ? "重试 "+endpoint.catalog.retry_at : ""}`);catalog.dataset.dimension="catalog";
      const key=endpoint.key.configured ? endpoint.key.masked ? "密钥：已掩码" : `密钥：末四位 ${endpoint.key.last4}` : "密钥：未配置";
      card.append(node("h3",endpoint.endpoint_id),observation,catalog,node("p",key),node("p",endpoint.auth_probe.available?"凭据探针可用":"无免费认证探针"));
      if(endpoint.auth_probe.available && endpoint.key.configured) {
        const button=node("button","验证凭据");button.dataset.probeEndpoint=endpoint.endpoint_id;button.dataset.focusKey="probe:"+endpoint.endpoint_id;button.disabled=!connected || pending.has(endpoint.endpoint_id);
        button.onclick=()=>void verifyCredentials(endpoint.endpoint_id,button);card.append(button);
      }
      if(pending.has(endpoint.endpoint_id))card.append(node("p","凭据测试正在提交，结果仍待核对。"));
      const more=details("endpoint:"+endpoint.endpoint_id,"端点详情");
      more.append(node("p",`base_url：${endpoint.base_url || "未提供"}`),node("p",`catalog_url：${endpoint.catalog_url || "未单独配置；沿 base_url 目录路径回落，不等于无目录能力"}`),node("p",`api_key_env：${endpoint.api_key_env || "未提供"}`),node("p",key),node("p",`观测时间：${endpoint.observation.checked_at || "尚无"}；方式：${endpoint.observation.checked_via || "尚无"}`));
      card.append(more);endpointGroup.append(card);
    }
    if(!(body.endpoints || []).length)endpointGroup.append(node("p","当前没有可展示的模型端点。"));
    for (const integration of body.integrations || []) {
      const card=node("article");card.className="connection-row";card.dataset.integration=integration.integration_id;
      const observed=integration.observation, key=integration.key;
      card.append(node("h3",integration.integration_id),node("p",`连接：${STATE_LABEL[observed.state] || observed.state} ${observed.reason || ""}`));
      if(observed.checked_at)card.append(node("p",`历史观测 ${observed.checked_at} ${observed.checked_via || ""}`));
      card.append(node("p",!key.configured?"密钥：未配置":key.masked?"密钥：已掩码":`密钥：末四位 ${key.last4}`),node("p","仅显式测试发送 /usage；探活免费未证实。测试连接不授予工具权限。"));
      if(integration.last_attempt)card.append(node("p",`最近尝试：${integration.last_attempt.reason || "未提供说明"} ${integration.last_attempt.attempted_at || integration.last_attempt.checked_at || ""}`));
      if(integration.retry_at)card.append(node("p",`下次可测试：${integration.retry_at}`));
      if(integration.search_retry_at)card.append(node("p",`搜索可重试：${integration.search_retry_at}`));
      const button=node("button","测试连接");button.dataset.probeEndpoint=integration.integration_id;button.dataset.focusKey="probe:"+integration.integration_id;
      button.disabled=!connected || integration.availability!=="configured" || pending.has(integration.integration_id);button.onclick=()=>void verifyCredentials(integration.integration_id,button);card.append(button);
      if(integration.availability!=="configured")card.append(node("p",`测试不可用：${integration.availability || "未知"}`));
      if(pending.has(integration.integration_id))card.append(node("p","测试连接正在提交；旧观测不代表本次结果。"));
      const more=details("integration:"+integration.integration_id,"集成详情");
      for(const scope of ["key","account"]) {
        const balance=observed.balances?.[scope];
        more.append(node("p",`${scope} 套餐用量：${balance?.plan_usage ?? "未报告"}；套餐上限：${balance?.plan_limit ?? "未报告"}`));
      }
      const tools=node("a","到工具页核对授权");tools.href="/tools";more.append(tools);card.append(more);integrationGroup.append(card);
    }
    if(!(body.integrations || []).length)integrationGroup.append(node("p","当前没有可展示的可选集成。"));
    const section=node("section");section.className="settings-group";section.setAttribute("aria-label","MCP 服务器");section.append(node("h2","MCP 服务器"));list.append(section);
    if (body.mcp) {
      const mcp=body.mcp;
      section.append(node("p",`配置来源：${mcp.config_path || "未配置"}`),node("p","启用配置允许启动本地程序；工具调用仍需在工具页逐项授权。命令不是沙箱，配置指纹不证明程序内容未变。"));
      if(mcp.error)section.append(node("p",`配置未应用或能力暂停：${mcp.error}`));
      if(!mcp.servers.length)section.append(node("p","未配置服务器；在状态目录 mcp.json 中显式设置 enabled: true 后应用。"));
      const apply=node("button","应用 mcp.json");apply.dataset.focusKey="mcp:apply";apply.disabled=!connected || pendingMCP;
      apply.onclick=()=>void operateMCP({action:"apply",server_key:null,token:mcp.token,operation_id:crypto.randomUUID()});section.append(apply);
      if(mcp.operation)section.append(node("p",`最近操作 ${mcp.operation.operation_id}：${mcp.operation.status}；操作结束不等于全部服务器成功。`));
      for(const server of mcp.servers) {
        const card=node("article");card.className="connection-row";card.dataset.mcpServer=server.server_key;
        card.append(node("h3",server.server_key),node("p",`启动许可：${server.enabled?"已启用":"未启用"}`),node("p",`连接：${STATE_LABEL[server.state] || server.state} ${server.reason || ""}`),node("p",`工具：可用 ${server.available_tools} / 总数 ${server.total_tools}；可用数不等于已授权数。`));
        if(server.reason==="restart_required")card.append(node("p","新环境已生效，MCP 尚待重连；旧工具不可调用。"));
        if(server.cleanup_incomplete)card.append(node("p","清理未完成，不能启动替代进程。"));
        if(server.directory_changed)card.append(node("p","目录可能变化；显式重连后重新发现。"));
        for(const [action,label] of [["reconnect","重连"],["cleanup","继续清理"]]) {
          const button=node("button",label);button.dataset.focusKey="mcp:"+action+":"+server.server_key;button.disabled=!connected || pendingMCP;
          button.onclick=()=>void operateMCP({action,server_key:server.server_key,token:mcp.token,operation_id:crypto.randomUUID()});card.append(button);
        }
        const more=details("mcp:"+server.server_key,"服务器详情");
        more.append(node("p",`server_key：${server.server_key}`));
        if(server.history)more.append(node("p",`历史连接：${server.history.state}；不代表当前就绪。`));
        for(const line of server.diagnostics || [])more.append(node("pre",line));
        const tools=node("a","到工具页核对授权");tools.href="/tools";more.append(tools);card.append(more);section.append(card);
      }
    } else section.append(node("p","MCP 状态未提供；不能推断维护成功。"));
    if(focusKey && focused && !focused.isConnected && document.activeElement===document.body) {
      const replacement=[...list.querySelectorAll('[data-focus-key]')].find(e=>e instanceof HTMLElement && e.dataset.focusKey===focusKey);
      if(replacement instanceof HTMLElement)replacement.focus({preventScroll:true});
    }

    return true;
  }

  /** @param {Wire} payload */
  async function operateMCP(payload) {
    if(pendingMCP || !alive || !connected || !csrf())return;
    pendingMCP=true;
    const epoch=generation;
    const attempt = ++sequence;
    ++noticeSequence;
    notice.textContent = "MCP 操作正在受理…";
    for(const button of list.querySelectorAll('button'))if(button instanceof HTMLButtonElement && button.dataset.focusKey?.startsWith('mcp:'))button.disabled=true;
    try {
      const response = await fetch("/api/connections/mcp", {
        method: "POST", headers: {"Content-Type": "application/json", "x-agent-alfred-csrf": csrf()},
        body: JSON.stringify(payload),
      });
      let result = await response.json();
      if (!response.ok) throw new Error(result.code || "操作未受理");
      if(result.operation_id!==payload.operation_id)throw new Error("操作回执身份不符，结果未确认");
      while (result.status === "running" && list.isConnected && attempt === sequence && epoch===generation) {
        notice.textContent = `MCP 操作 ${result.operation_id}：准备 / 清理 / 发布中`;
        await new Promise(resolve => setTimeout(resolve, 250));
        const progress = await fetch("/api/connections/mcp/operation?" + new URLSearchParams({operation_id: payload.operation_id}));
        result = await progress.json();
        if (!progress.ok || result.operation_id!==payload.operation_id) throw new Error("操作进度未知，请核对当前状态");
      }
      if (attempt === sequence && list.isConnected && epoch===generation) {
        notice.textContent = `MCP 操作 ${result.status} ${result.error || ""}；请核对各服务器结果。`;
        channel.postMessage({instance});
      }
    } catch (error) {
      if (attempt === sequence && list.isConnected && epoch===generation) {
        notice.replaceChildren(node("span", String(error)));
        const retry = node("button", "重试同一 MCP 操作");
        retry.onclick = () => {if(epoch===generation)void operateMCP(payload);};
        notice.append(retry);
      }
    } finally { if(epoch===generation){pendingMCP=false;await refresh();} }
  }

  async function refresh() {
    if (!alive || !list.isConnected) {channel.close(); return;}
    // Reads are not new user operations and cannot invalidate their receipts.
    const attempt = sequence;
    const read = ++readSequence;
    const noticeAtStart = noticeSequence;
    try {
      const response = await fetch("/api/connections",{signal:reads.signal});
      const body = await response.json();
      if (!response.ok) throw new Error("read_unavailable");
      if (attempt === sequence && read === readSequence) render(body);
    } catch {
      if (attempt === sequence && read === readSequence && noticeAtStart === noticeSequence && list.isConnected) notice.textContent = "连接状态读取失败，请重试。";
    }
  }
  void refresh();
  /** @param {string} value */
  function sync(value) {
    if (value !== instance && adoptInstance(value)) {
      list.replaceChildren(node("p", "连接状态更新中…"));
      void refresh();
    }
  }
  const stop=dashboard.subscribeState(state=>{
    const changed=Boolean(state.connected)!==connected;
    connected=Boolean(state.connected);
    if(!connected && changed){
      generation++;sequence++;readSequence++;noticeSequence++;
      const hadPending=pending.size || pendingMCP || pendingReread;
      pending.clear();pendingMCP=false;pendingReread=false;
      notice.textContent=hadPending?"连接中断，原操作结果未确认；请核对，不会自动重送。":"连接中断；当前连接状态仅为旧观察，维护操作暂停。";
    }
    if(changed && current)render(current);
    if(changed && connected)void refresh();
  });
  return {refresh, sync, close() {
    alive=false;generation++;reads.abort();stop();
    ++sequence; channel.close();
    window.removeEventListener("focus", focused);
  }};
}
