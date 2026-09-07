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
      if (response.ok) render(body);
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
      if (response.ok) render(body);
      else if (button.isConnected)
        button.insertAdjacentElement(
          "afterend",
          node("p", body.code || "error"),
        );
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }

  /** @param {Wire} body */
  function render(body) {
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
  }

  void fetch("/api/connections")
    .then((response) => response.json())
    .then(render);
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
