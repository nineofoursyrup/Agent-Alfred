import {topologyView} from "./topology.js";
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
    this.cursor = "";
    this.busy = false;
    this.finished = false;
    this.button = node("button", label);
    this.label = label;
    this.button.addEventListener("click", () => void this.load());
    root.append(this.button);
  }
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
        this.path + "?" + new URLSearchParams(params),
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
        open.addEventListener("click", () =>
          showSession(preview, session, resume),
        );
      }
    },
    "更多会话",
  );
  void pager.load();
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
}

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
  root.append(list);
  const reread = node("button", "重新读取 .env");
  reread.addEventListener("click", () => void postReread());
  root.append(reread);

  async function postReread() {
    const token = csrf();
    if (!token) return;
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
      if (response.ok) acceptMutation(body, attempt);
      else mutationFailure(body.code || "error", attempt);
    } catch {
      mutationFailure("重读结果未确认，请核对当前状态后重试。", attempt);
    } finally {
      reread.disabled = false;
    }
  }

  /** @param {string} endpointId @param {HTMLButtonElement} button */
  async function verifyCredentials(endpointId, button) {
    const token = csrf();
    if (!token) return;
    if (button.disabled) return;
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
      if (response.ok) acceptMutation(body, attempt);
      else mutationFailure(body.code || "error", attempt);
    } catch {
      mutationFailure("测试结果未确认，请核对当前状态。", attempt);
    } finally {
      if (button.isConnected) button.disabled = false;
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
      instance = value; revision = -1;
      ++sequence; ++noticeSequence;
      notice.textContent = "";
      configurationNotice.textContent = "";
    }

    return true;
  }

  /** @param {Wire} body */
  function render(body) {
    if (!list.isConnected || !adoptInstance(body.process_instance_id || instance)) return false;
    const incoming = body.connections_revision ?? body.integration_revision ?? 0;
    if (incoming < revision) return false;
    revision = incoming;
    // This current configuration fact is independent of operation receipts.
    configurationNotice.textContent = body.integration_application === "not_applied"
      ? "配置未能一致生效，外部能力已暂停；请修复后重新读取 .env。" : "";
    list.replaceChildren();
    for (const endpoint of body.endpoints) {
      const card = node("article");
      card.className = "card";
      card.setAttribute("data-endpoint", endpoint.endpoint_id);
      const title = node("h2", endpoint.endpoint_id);
      const observation = node(
        "p",
        `连接：${STATE_LABEL[endpoint.observation.state] || endpoint.observation.state}`,
      );
      observation.setAttribute("data-dimension", "connection");
      if (endpoint.observation.checked_at)
        observation.append(
          document.createTextNode(
            ` ${endpoint.observation.checked_at} ${endpoint.observation.checked_via || ""}`,
          ),
        );
      if (endpoint.observation.reason)
        observation.append(
          document.createTextNode(` ${endpoint.observation.reason}`),
        );
      const catalog = node(
        "p",
        `目录：${CATALOG_LABEL[endpoint.catalog.health] || endpoint.catalog.health}`,
      );
      catalog.setAttribute("data-dimension", "catalog");
      if (endpoint.catalog.last_success_at)
        catalog.append(
          document.createTextNode(` 上次成功 ${endpoint.catalog.last_success_at}`),
        );
      if (endpoint.catalog.last_error)
        catalog.append(
          document.createTextNode(` ${endpoint.catalog.last_error}`),
        );
      if (endpoint.catalog.retry_at)
        catalog.append(
          document.createTextNode(` 重试 ${endpoint.catalog.retry_at}`),
        );
      const key = endpoint.key.configured
        ? endpoint.key.masked
          ? "密钥：已掩码"
          : `密钥：末四位 ${endpoint.key.last4}`
        : "密钥：未配置";
      const probe = node(
        "p",
        endpoint.auth_probe.available
          ? "凭据探针可用"
          : endpoint.auth_probe.label,
      );
      card.append(title, observation, catalog, node("p", key), probe);
      if (endpoint.auth_probe.available && endpoint.key.configured) {
        const button = node("button", "验证凭据");
        button.addEventListener("click", () =>
          void verifyCredentials(endpoint.endpoint_id, button),
        );
        card.append(button);
      }
      list.append(card);
    }
    for (const integration of body.integrations || []) {
      const card = node("article");
      card.className = "card";
      card.setAttribute("data-integration", integration.integration_id);
      const observed = integration.observation;
      card.append(node("h2", integration.integration_id), node("p",
        `连接：${STATE_LABEL[observed.state] || observed.state} ${observed.reason || ""}`));
      if (observed.checked_at) card.append(node("p", `历史观测 ${observed.checked_at} ${observed.checked_via}`));
      const key = integration.key;
      card.append(node("p", !key.configured ? "密钥：未配置" : key.masked ? "密钥：已掩码" : `密钥：末四位 ${key.last4}`));
      card.append(node("p", "仅显式测试发送 /usage；探活免费未证实。授权请到工具页设置。"));
      if (integration.last_attempt?.reason) card.append(node("p", `本次说明：${integration.last_attempt.reason}`));
      if (integration.retry_at) card.append(node("p", `下次可测试：${integration.retry_at}`));
      if (integration.search_retry_at) card.append(node("p", `搜索可重试：${integration.search_retry_at}`));
      for (const scope of ["key", "account"]) {
        const balance = observed.balances?.[scope];
        if (balance) card.append(node("p", `${scope} 套餐用量：${balance.plan_usage ?? "未报告"}；套餐上限：${balance.plan_limit ?? "未报告"}`));
      }
      const button = node("button", "测试连接");
      button.disabled = integration.availability !== "configured";
      button.addEventListener("click", () => void verifyCredentials(integration.integration_id, button));
      card.append(button);
      list.append(card);
    }

    if (body.mcp) {
      const mcp = body.mcp;
      const section = node("section"); section.setAttribute("aria-label", "MCP 服务器");
      section.append(node("h2", "本地 MCP"), node("p", `配置来源：${mcp.config_path || "未配置"}`),
        node("p", "启用配置允许启动本地程序；工具调用仍需在工具页逐项授权。命令不是沙箱，配置指纹不证明程序内容未变。"));
      if (mcp.error) section.append(node("p", `配置未应用或能力暂停：${mcp.error}`));
      if (!mcp.servers.length) section.append(node("p", "未配置服务器；在状态目录 mcp.json 中显式设置 enabled: true 后应用。"));
      const apply = node("button", "应用 mcp.json");
      apply.onclick = () => void operateMCP({action:"apply", server_key:null, token:mcp.token, operation_id:crypto.randomUUID()});
      section.append(apply);
      if (mcp.operation) section.append(node("p", `最近操作 ${mcp.operation.operation_id}：${mcp.operation.status}`));
      for (const server of mcp.servers) {
        const card = node("article"); card.className = "card"; card.setAttribute("data-mcp-server", server.server_key);
        card.append(node("h3", server.server_key), node("p", `启动许可：${server.enabled ? "已启用" : "未启用"}`),
          node("p", `连接：${STATE_LABEL[server.state] || server.state} ${server.reason || ""}`),
          node("p", `工具：可用 ${server.available_tools} / 总数 ${server.total_tools}`));
        if (server.reason === "restart_required") card.append(node("p", "新环境已生效，MCP 尚待重连；旧工具不可调用。"));
        if (server.cleanup_incomplete) card.append(node("p", "清理未完成，不能启动替代进程。"));
        if (server.directory_changed) card.append(node("p", "目录可能变化；显式重连后重新发现。"));
        if (server.history) card.append(node("p", `历史连接：${server.history.state}；不代表当前就绪。`));
        for (const line of server.diagnostics || []) card.append(node("pre", line));
        for (const [action, label] of [["reconnect", "重连"], ["cleanup", "继续清理"]]) {
          const button = node("button", label);
          button.onclick = () => void operateMCP({action, server_key:server.server_key, token:mcp.token, operation_id:crypto.randomUUID()});
          card.append(button);
        }
        section.append(card);
      }
      list.append(section);
    }

    return true;
  }

  /** @param {Wire} payload */
  async function operateMCP(payload) {
    const attempt = ++sequence;
    ++noticeSequence;
    notice.textContent = "MCP 操作正在受理…";
    try {
      const response = await fetch("/api/connections/mcp", {
        method: "POST", headers: {"Content-Type": "application/json", "x-agent-alfred-csrf": csrf()},
        body: JSON.stringify(payload),
      });
      let result = await response.json();
      if (!response.ok) throw new Error(result.code || "操作未受理");
      while (result.status === "running" && list.isConnected && attempt === sequence) {
        notice.textContent = `MCP 操作 ${result.operation_id}：准备 / 清理 / 发布中`;
        await new Promise(resolve => setTimeout(resolve, 250));
        const progress = await fetch("/api/connections/mcp/operation?" + new URLSearchParams({operation_id: payload.operation_id}));
        result = await progress.json();
        if (!progress.ok) throw new Error("操作进度未知，请核对当前状态");
      }
      if (attempt === sequence && list.isConnected) {
        notice.textContent = `MCP 操作 ${result.status} ${result.error || ""}；请核对各服务器结果。`;
        channel.postMessage({instance});
      }
    } catch (error) {
      if (attempt === sequence && list.isConnected) {
        notice.replaceChildren(node("span", String(error)));
        const retry = node("button", "重试同一 MCP 操作");
        retry.onclick = () => void operateMCP(payload);
        notice.append(retry);
      }
    } finally { await refresh(); }
  }

  async function refresh() {
    if (!list.isConnected) {channel.close(); return;}
    // Reads are not new user operations and cannot invalidate their receipts.
    const attempt = sequence;
    const read = ++readSequence;
    const noticeAtStart = noticeSequence;
    try {
      const response = await fetch("/api/connections");
      const body = await response.json();
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
  return {refresh, sync, close() {
    ++sequence; channel.close();
    window.removeEventListener("focus", focused);
  }};
}

/** @param {HTMLElement} root @param {()=>string} csrf */
export function modelsPage(root, csrf) {
  const list = node("div");
  list.setAttribute("aria-label", "模型候选");
  root.append(list);
  /** @type {Wire|null} */
  let current = null;

  /** @param {string} op @param {Record<string, unknown>} fields */
  async function mutate(op, fields) {
    const token = csrf();
    if (!token || current == null) return;
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-agent-alfred-csrf": token,
      },
      body: JSON.stringify({
        op,
        expected_revision: current.revision,
        ...fields,
      }),
    });
    const body = await response.json();
    if (response.ok) render(body);
  }

  /** @param {string} endpointId */
  async function expand(endpointId) {
    const response = await fetch(
      "/api/models?" + new URLSearchParams({ expand: endpointId }),
    );
    const body = await response.json();
    if (response.ok) render(body);
  }

  /** @param {string} endpointId @param {HTMLButtonElement} button */
  async function refreshCatalog(endpointId, button) {
    if (button.disabled) return;
    button.disabled = true;
    try {
      const response = await fetch(
        "/api/models?" +
          new URLSearchParams({ expand: endpointId, refresh: "1" }),
      );
      const body = await response.json();
      if (response.ok) render(body);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }

  /** @param {Wire} body */
  function render(body) {
    current = body;
    list.replaceChildren();
    if (body.status && body.status !== "ok") {
      list.append(node("p", `设置不可用：${body.status}`));
      return;
    }
    for (const group of body.endpoints) {
      const section = node("section");
      section.setAttribute("data-endpoint", group.endpoint_id);
      const header = node("h2", group.endpoint_id);
      const observation = node(
        "p",
        `连接：${STATE_LABEL[group.observation?.state] || group.observation?.state || ""}`,
      );
      observation.setAttribute("data-dimension", "connection");
      if (group.observation?.checked_at)
        observation.append(
          document.createTextNode(
            ` ${group.observation.checked_at} ${group.observation.checked_via || ""}`,
          ),
        );
      const catalog = node(
        "p",
        `目录：${CATALOG_LABEL[group.catalog.health] || group.catalog.health}`,
      );
      catalog.setAttribute("data-dimension", "catalog");
      if (group.catalog.last_success_at)
        catalog.append(
          document.createTextNode(
            ` 上次成功 ${group.catalog.last_success_at}`,
          ),
        );
      if (group.catalog.last_error)
        catalog.append(document.createTextNode(` ${group.catalog.last_error}`));
      if (group.catalog.retry_at)
        catalog.append(
          document.createTextNode(` 重试 ${group.catalog.retry_at}`),
        );
      const open = node("button", "展开此端点");
      open.addEventListener("click", () => void expand(group.endpoint_id));
      const refresh = node("button", "刷新目录");
      refresh.addEventListener("click", () =>
        void refreshCatalog(group.endpoint_id, refresh),
      );
      section.append(header, observation, catalog, open, refresh);
      for (const model of group.models) {
        const row = node("article");
        row.className = "card";
        row.setAttribute("data-model", `${model.endpoint_id}:${model.model_id}`);
        const support = node("p", model.support_label);
        support.setAttribute("data-dimension", "support");
        row.append(
          node("h3", model.display_name || model.model_id),
          support,
        );
        if (model.disable_code) {
          row.dataset.disableCode = model.disable_code;
          row.dataset.disableDimension = model.disable_dimension;
        }
        const assignedPrimary =
          body.assignments?.primary?.endpoint_id === model.endpoint_id &&
          body.assignments?.primary?.model_id === model.model_id;
        const assignedGate =
          body.assignments?.retrieval_gate?.endpoint_id === model.endpoint_id &&
          body.assignments?.retrieval_gate?.model_id === model.model_id;
        if (model.assignable) {
          const assign = node("button", "指派为主模型");
          assign.addEventListener("click", () =>
            void mutate("assign", {
              slot: "primary",
              endpoint_id: model.endpoint_id,
              model_id: model.model_id,
            }),
          );
          row.append(assign);
          const gate = node("button", "指派为检索门");
          gate.title = "同时用于 Skill 自动选择、记忆检索门与消息分类";
          gate.addEventListener("click", () =>
            void mutate("assign", {
              slot: "retrieval_gate",
              endpoint_id: model.endpoint_id,
              model_id: model.model_id,
            }),
          );
          row.append(gate);
        } else if (model.pinned) {
          const blocked = node("button", "不可指派");
          blocked.disabled = true;
          if (model.disable_code)
            blocked.dataset.disableCode = model.disable_code;
          row.append(blocked);
        }
        if (!model.pinned) {
          const pin = node("button", "钉选");
          pin.addEventListener("click", () =>
            void mutate("pin", {
              endpoint_id: model.endpoint_id,
              model_id: model.model_id,
            }),
          );
          row.append(pin);
        } else {
          const unpin = node("button", "取消钉选");
          unpin.disabled = assignedPrimary || assignedGate;
          unpin.addEventListener("click", () =>
            void mutate("unpin", {
              endpoint_id: model.endpoint_id,
              model_id: model.model_id,
            }),
          );
          row.append(unpin);
          const style = node("select");
          style.setAttribute("aria-label", "线路形状");
          for (const value of ["", "openai", "anthropic"]) {
            const option = node("option", value || "内置");
            option.value = value;
            if ((model.wire_style_source === "user_declared" ? model.wire_style : "") === value)
              option.selected = true;
            style.append(option);
          }
          style.addEventListener("change", () => {
            if (style.value)
              void mutate("style", {
                endpoint_id: model.endpoint_id,
                model_id: model.model_id,
                wire_style: style.value,
              });
          });
          row.append(style);
          const name = node("input");
          name.setAttribute("aria-label", "显示名");
          if (model.display_name) name.value = model.display_name;
          const saveName = node("button", "保存显示名");
          saveName.addEventListener("click", () =>
            void mutate("display", {
              endpoint_id: model.endpoint_id,
              model_id: model.model_id,
              display_name: name.value || null,
            }),
          );
          row.append(name, saveName);
          for (const dimension of [
            "uncached_input",
            "cache_read",
            "cache_write",
            "output",
          ]) {
            const price = node("input");
            price.setAttribute("aria-label", dimension);
            price.placeholder = dimension;
            const current =
              model.price_override && model.price_override[dimension];
            if (current != null) price.value = String(current);
            price.addEventListener("change", () =>
              void mutate("price", {
                endpoint_id: model.endpoint_id,
                model_id: model.model_id,
                dimension,
                value: price.value === "" ? null : price.value,
              }),
            );
            row.append(price);
          }
        }
        if (model.probe) {
          const probe = node("button", "测试真实调用");
          probe.disabled = !model.probe_enabled;
          probe.addEventListener("click", () =>
            void inferenceProbe(model.endpoint_id, model.model_id),
          );
          row.append(probe, node("p", "可能产生费用"));
        }
        section.append(row);
      }
      list.append(section);
    }
  }

  /** @param {string} endpointId @param {string} modelId */
  async function inferenceProbe(endpointId, modelId) {
    const token = csrf();
    if (!token) return;
    await fetch("/api/runs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-agent-alfred-csrf": token,
      },
      body: JSON.stringify({
        message: "inference probe",
        purpose: "inference_probe",
        endpoint_id: endpointId,
        model_id: modelId,
      }),
    });
  }

  void fetch("/api/models")
    .then((response) => response.json())
    .then(render);
}

/** @param {HTMLElement} root @param {()=>string} csrf @param {()=>Wire} runtime @param {import("./memory.js").MemorySync} memory */
export function behaviourPage(root, csrf, runtime, memory) {
  const routing = node("section"); routing.setAttribute("aria-label", "消息分流");
  routing.append(node("h2", "消息分流"), node("p", "持续设置 · CLI/Web 共享。默认关闭；保存后下一 Run 生效。回复进入当前会话，静默时只记用户消息，不生成助手消息或长期记忆。"));
  root.append(routing);
  const aggregation = aggregationForm(root, csrf, runtime, memory);
  let state = /** @type {Wire} */ ({});
  const label = node('label', '启用消息分流');
  const enabled = document.createElement('input');
  enabled.type = 'checkbox';
  enabled.disabled = true;
  label.prepend(enabled);
  const notice = node('p'); notice.setAttribute('role', 'status');
  const save = node('button', '保存设置'); save.disabled = true;
  const refresh = node('button', '刷新设置');
  const recover = node('button', '备份原文件并恢复为关闭'); recover.hidden = true;
  const actual = node('p'); actual.setAttribute('role', 'status');
  routing.append(actual, node('p', '默认关闭。启用后识别纯问候、致谢及明确无需回复的消息；实际任务仍进入完整回答。'),
    label, save, refresh, recover, notice);
  const views = [topologyView(routing, "message_routing", runtime, memory), topologyView(aggregation, "manual_aggregation", runtime, memory)];

  async function read() {
    try {
      const response = await fetch('/api/behaviour');
      if (!response.ok) throw new Error('读取失败');
      state = await response.json();
      if (!root.isConnected) return;
      actual.textContent = state.status === 'ok' ? `已保存的分流设置：${state.enabled ? '开启' : '关闭'}。能查看结构不代表已经启用或可以成功执行。` : `分流设置未知 / ${state.status}。`;
      enabled.checked = state.enabled;
      enabled.disabled = state.status !== 'ok';
      save.disabled = state.status !== 'ok';
      recover.hidden = state.status === 'ok' || !state.fingerprint;
      notice.textContent = state.status === 'ok' ? '设置在下一 Run 生效。'
        : `配置不可用（${state.status}）；普通聊天仍可使用。恢复会先备份原文件。`;
    } catch { notice.textContent = '设置读取失败，请刷新。'; }
  }
  /** @param {string} action */
  async function write(action) {
    save.disabled = true; recover.disabled = true;
    try {
      const response = await fetch('/api/behaviour', {
        method:'POST', headers:{'Content-Type':'application/json', 'x-agent-alfred-csrf':csrf()},
        body:JSON.stringify({action, expected_revision:state.revision,
          enabled:enabled.checked, fingerprint:state.fingerprint}),
      });
      const result = await response.json();
      if (!root.isConnected) return;
      if (!response.ok) {
        notice.textContent = `未保存（${result.code}${result.cause ? ' / ' + result.cause : ''}）。选择已保留，请刷新后重试。${result.backup_path ? '备份：' + result.backup_path : ''}`;
        return;
      }
      state = result; enabled.checked = result.enabled;
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
  return {close(){for (const view of views) view.close();}};
}
