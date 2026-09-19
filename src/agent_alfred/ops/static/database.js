import { node } from "./dom.js";
/** @typedef {Record<string, any>} Wire */
/** @typedef {{generation:number, id:string, token:string, controller:AbortController,
 * cancelled:boolean, cancelSent:boolean}} Execution */

const STATES = {
  checking: "核验中",
  ready: "可执行",
  unavailable: "不可用",
  busy: "忙",
  working: "准备／执行中",
  cancelling: "已请求取消",
  complete: "结果完整",
  truncated: "结果截断",
  empty: "空结果",
  sql_error: "执行错误",
  failed: "执行失败",
  unused: "尚未执行",
  timeout: "超时",
  invalidated: "失效",
  missing: "结果未收到",
  stopped: "已实际停止",
  cleanup: "清理失败",
};

/**
 * @param {HTMLElement} root
 * @param {{csrf:()=>string, instance:()=>string, connected:()=>boolean}} ctx
 */
export function databasePage(root, ctx) {
  const status = node("p", STATES.checking);
  status.setAttribute("aria-live", "polite");
  const notice = node("p");
  notice.className = "muted";
  notice.textContent =
    "查询受保护的临时数据。SQL 的 WHERE／LIMIT 不缩小准备范围。结果有上限，不是数据库总量。";
  const catalog = node("section");
  catalog.setAttribute("aria-label", "诊断对象目录");
  const editor = node("textarea");
  editor.setAttribute("aria-label", "SQL");
  editor.rows = 8;
  editor.placeholder = "SELECT * FROM diag_sessions LIMIT 20";
  const run = node("button", "执行");
  const cancel = node("button", "取消");
  const copySql = node("button", "复制 SQL");
  const copyResult = node("button", "复制结果");
  const result = node("section");
  result.setAttribute("aria-label", "查询结果");
  root.append(status, notice, catalog, editor, run, cancel, copySql, copyResult, result);

  let generation = 0;
  let pageGeneration = 0;
  let catalogBody = /** @type {Wire|null} */ (null);
  let execution = /** @type {Execution|null} */ (null);
  /** @type {Wire|null} */
  let resultMeta = null;
  let inFlight = false;
  let sqlDraft = editor.value;
  let rows = /** @type {Wire[][]} */ ([]);
  let columns = /** @type {string[]} */ ([]);
  let page = 0;
  let alive = true;
  let online = ctx.connected();

  /** @param {keyof typeof STATES | string} key @param {string} [extra] */
  function setState(key, extra) {
    status.dataset.state = key;
    const label = key in STATES ? STATES[/** @type {keyof typeof STATES} */ (key)] : key;
    status.textContent = extra ? `${label} · ${extra}` : label;
  }

  function clearResult() {
    rows = [];
    columns = [];
    page = 0;
    resultMeta = null;
    result.replaceChildren();
  }

  async function loadCatalog() {
    const mine = ++generation;
    setState("checking");
    try {
      const response = await fetch("/api/database", { cache: "no-store" });
      const body = await response.json();
      if (!alive || mine !== generation) return;
      if (body.instance_id && body.instance_id !== ctx.instance()) {
        setState("unavailable", "实例已更换");
        return;
      }
      catalogBody = body;
      renderCatalog(body);
      if (!body.available) {
        setState(body.reason === "cleanup_failed" ? "cleanup" : "unavailable", body.reason || "");
        run.disabled = true;
        return;
      }
      if (!online || !ctx.connected()) {
        setState("unavailable", "连接中断");
        run.disabled = true;
        return;
      }
      setState("ready");
      run.disabled = false;
    } catch {
      if (alive && mine === generation) setState("unavailable", "网络失败");
    }
  }

  /** @param {Wire} body */
  function renderCatalog(body) {
    catalog.replaceChildren();
    catalog.append(node("p", `当前实例固定。记忆修订 ${body.memory_revision} · 保护规则版本 ${body.protection_version}`));
    for (const object of body.objects || []) {
      const card = node("article");
      card.className = "card";
      const title = node("h2", object.name);
      const insert = node("button", "写入编辑器");
      insert.addEventListener("click", () => {
        editor.value = object.example;
        sqlDraft = editor.value;
      });
      card.append(
        title,
        node("p", object.source),
        node("p", object.notes || ""),
        node(
          "p",
          /** @type {Wire[]} */ (object.columns || [])
            .map(
              (column) =>
                `${column.name} ${column.type}${column.nullable ? "" : " NOT NULL"}${column.identity ? " · 身份" : ""}`,
            )
            .join(" · "),
        ),
        insert,
      );
      catalog.append(card);
    }
  }

  function renderRows() {
    result.replaceChildren();
    if (!columns.length && !rows.length && !resultMeta) return;
    const bits = [];
    bits.push(rows.length ? `返回 ${rows.length} 行（本页展示，非库总量）` : "0 行");
    if (resultMeta?.read_at) bits.push(`读取 ${resultMeta.read_at}`);
    const sources = resultMeta?.objects;
    if (Array.isArray(sources) && sources.length) bits.push(`来源 ${sources.join("、")}`);
    if (resultMeta?.protection_version) bits.push(`保护 ${resultMeta.protection_version}`);
    const coverage = resultMeta?.coverage;
    if (coverage?.notice) bits.push(String(coverage.notice));
    const attempts = coverage?.attempts;
    if (attempts && typeof attempts === "object") {
      bits.push(
        `attempts recorded=${attempts.recorded ?? 0} empty=${attempts.recorded_empty ?? 0} unrecorded=${attempts.unrecorded ?? 0} in_progress=${attempts.in_progress_runs ?? 0}`,
      );
    }
    const meta = node("p", bits.join(" · "));
    result.append(meta);
    if (!columns.length) return;
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const hr = document.createElement("tr");
    for (const name of columns) {
      const th = document.createElement("th");
      th.textContent = name;
      hr.append(th);
    }
    head.append(hr);
    table.append(head);
    const body = document.createElement("tbody");
    const start = page * 100;
    for (const row of rows.slice(start, start + 100)) {
      const tr = document.createElement("tr");
      for (const cell of /** @type {Wire[]} */ (row)) {
        const td = document.createElement("td");
        td.append(renderCell(cell));
        tr.append(td);
      }
      body.append(tr);
    }
    table.append(body);
    result.append(table);
    if (rows.length > 100) {
      const prev = node("button", "上一页");
      const next = node("button", "下一页");
      prev.disabled = page === 0;
      next.disabled = (page + 1) * 100 >= rows.length;
      prev.onclick = () => {
        page -= 1;
        renderRows();
      };
      next.onclick = () => {
        page += 1;
        renderRows();
      };
      result.append(prev, next);
    }
  }

  /** @param {Wire} cell */
  function renderCell(cell) {
    if (!cell || typeof cell !== "object") return document.createTextNode("");
    if (cell.type === "null") {
      const mark = node("span", "NULL");
      mark.className = "sql-null";
      return mark;
    }
    if (cell.type === "integer") return document.createTextNode(cell.value);
    if (cell.type === "real") return document.createTextNode(String(cell.value));
    if (cell.type === "blob") return document.createTextNode(`BLOB ${cell.hex} (${cell.byte_length} 字节)`);
    const text = node("span", cell.value ?? "");
    return text;
  }

  /** @param {Execution} attempt */
  function ownsPage(attempt) {
    return alive && execution === attempt && attempt.generation === pageGeneration;
  }

  async function execute() {
    if (inFlight || !online || !ctx.connected() || !catalogBody?.available) return;
    const token = ctx.csrf();
    if (!token) return;
    const attempt = {
      generation: ++pageGeneration, id: "", token,
      controller: new AbortController(), cancelled: false, cancelSent: false,
    };
    execution = attempt;
    const catalogAtStart = catalogBody;
    const sql = editor.value;
    inFlight = true;
    run.disabled = true;
    clearResult();
    setState("working");
    try {
      // Keep issuance readable after a cancel click: its late handle must be
      // retired, never mistaken for the preceding execution's handle.
      const issued = await fetch("/api/database/queries", {
        method: "POST", cache: "no-store", keepalive: true,
        headers: {"Content-Type": "application/json", "x-agent-alfred-csrf": token},
        body: "{}",
      });
      const handle = await issued.json();
      if (!issued.ok) {
        if (ownsPage(attempt)) {
          if (attempt.cancelled) setState("stopped");
          else fail(handle);
        }
        return;
      }
      attempt.id = handle.query_id;
      if (!ownsPage(attempt) || attempt.cancelled) {
        await cancelHandle(attempt, true);
        return;
      }
      const executed = await fetch(`/api/database/queries/${encodeURIComponent(attempt.id)}/execute`, {
        method: "POST", cache: "no-store", signal: attempt.controller.signal,
        headers: {"Content-Type": "application/json", "x-agent-alfred-csrf": token},
        body: JSON.stringify({
          instance_id: catalogAtStart.instance_id,
          memory_revision: catalogAtStart.memory_revision,
          protection_version: catalogAtStart.protection_version,
          sql,
        }),
      });
      const payload = await executed.json();
      if (!ownsPage(attempt) || attempt.cancelled) return;
      if (!executed.ok) {fail(payload); return;}
      if (
        payload.instance_id !== catalogBody?.instance_id ||
        payload.memory_revision !== catalogBody?.memory_revision ||
        payload.protection_version !== catalogBody?.protection_version
      ) {
        setState("invalidated");
        clearResult();
        return;
      }
      columns = payload.columns || [];
      rows = payload.rows || [];
      resultMeta = payload;
      page = 0;
      renderRows();
      if (payload.truncated) setState("truncated", (payload.truncation_reasons || []).join("、"));
      else if (!rows.length) setState("empty");
      else setState("complete");
    } catch {
      if (!ownsPage(attempt) || attempt.cancelled) return;
      if (attempt.id) void probeMissing(attempt);
      else setState("unavailable", "网络失败");
    } finally {
      if (ownsPage(attempt)) {
        inFlight = false;
        run.disabled = !online || !ctx.connected() || !catalogBody?.available;
      }
    }
  }

  /** @param {Execution} attempt @param {Wire} body */
  function showTerminal(attempt, body) {
    if (!ownsPage(attempt)) return;
    // Execution outcome and cleanup are independent facts. A terminal
    // outcome does not imply failed cleanup or successful body delivery.
    if (body.cleanup === "failed" || body.code === "cleanup_failed") {
      setState("cleanup");
      return;
    }
    const pending = body.cleanup === "pending" ? "清理中" : undefined;
    switch (body.status) {
      case "completed":
        setState(attempt.cancelled ? "complete" : "missing",
          [attempt.cancelled ? "取消前已完成" : "", pending].filter(Boolean).join(" · "));
        break;
      case "failed": setState("failed", pending); break;
      case "timed_out": setState("timeout", pending); break;
      case "invalidated": setState("invalidated", pending); break;
      case "cancelled":
        setState(body.cleanup === "released" ? "stopped" : "cancelling");
        break;
      case "stopping": setState("cancelling"); break;
      case "prepared":
      case "running": setState(attempt.cancelled ? "cancelling" : "working"); break;
      case "unused": setState(attempt.cancelled ? "cancelling" : "unused"); break;
      default:
        if (body.code === "handle_expired") setState("unavailable", "句柄已过期");
        else if (body.code === "not_found") setState("unavailable", "没有这条查询记录");
        else if (body.code === "instance_changed") setState("unavailable", "实例已更换");
        else fail(body);
    }
  }

  /** @param {Execution} attempt */
  async function probeMissing(attempt) {
    try {
      const response = await fetch(`/api/database/queries/${encodeURIComponent(attempt.id)}`, {
        cache: "no-store",
      });
      showTerminal(attempt, await response.json());
    } catch {
      if (ownsPage(attempt)) setState("unavailable", "网络失败");
    }
  }

  /** @param {Execution} attempt @param {boolean} [keepalive] */
  async function cancelHandle(attempt, keepalive = false) {
    if (!attempt.id || attempt.cancelSent) return;
    attempt.cancelSent = true;
    try {
      const response = await fetch(`/api/database/queries/${encodeURIComponent(attempt.id)}/cancel`, {
        method: "POST", cache: "no-store", keepalive,
        headers: {"Content-Type": "application/json", "x-agent-alfred-csrf": attempt.token},
        body: "{}",
      });
      showTerminal(attempt, await response.json());
    } catch {
      if (ownsPage(attempt)) void probeMissing(attempt);
    }
  }

  /** @param {Wire} body */
  function fail(body) {
    const code = body?.code;
    if (code === "database_busy") setState("busy");
    else if (code === "query_timeout") setState("timeout");
    else if (code === "query_cancelled") setState("stopped");
    else if (code === "data_invalidated") setState("invalidated");
    else if (code === "cleanup_failed") setState("cleanup");
    else if (code === "sql_error" || code === "sql_rejected") setState("sql_error", body.detail);
    else if (code === "database_unavailable") setState("unavailable", body.detail);
    else setState("sql_error", body?.detail || code || "执行错误");
    clearResult();
  }

  function requestCancel() {
    const attempt = execution;
    if (!attempt || !ownsPage(attempt)) return;
    attempt.cancelled = true;
    clearResult();
    setState("cancelling");
    if (attempt.id) {
      attempt.controller.abort();
      void cancelHandle(attempt);
    }
  }

  function retireExecution() {
    pageGeneration += 1;
    inFlight = false;
    const attempt = execution;
    if (attempt) {
      attempt.cancelled = true;
      attempt.controller.abort();
      void cancelHandle(attempt, true);
    }
  }

  run.addEventListener("click", () => void execute());
  cancel.addEventListener("click", () => void requestCancel());
  copySql.addEventListener("click", () => void navigator.clipboard.writeText(editor.value));
  copyResult.addEventListener("click", () =>
    void navigator.clipboard.writeText(result.innerText),
  );
  editor.addEventListener("input", () => {
    sqlDraft = editor.value;
  });

  void loadCatalog();

  return {
    sync() {
      if (!alive || online) return;
      online = true;
      void loadCatalog();
    },
    /** @param {string} kind */
    invalidate(kind) {
      if (kind === "protection" || kind === "memory") {
        retireExecution();
        clearResult();
        editor.value = sqlDraft;
        setState("invalidated");
        void loadCatalog();
      }
    },
    disconnect() {
      online = false;
      generation += 1;
      run.disabled = true;
      retireExecution();
      clearResult();
      editor.value = sqlDraft;
      setState("unavailable", "连接中断");
    },
    suspend() {
      generation += 1;
      retireExecution();
      run.disabled = true;
      catalogBody = null;
      clearResult();
      editor.value = sqlDraft = "";
    },
    restoredFromCache() {
      retireExecution();
      clearResult();
      editor.value = "";
      sqlDraft = "";
      void loadCatalog();
    },
    close() {
      alive = false;
      generation += 1;
      clearResult();
      editor.value = sqlDraft = "";
      catalogBody = null;
      retireExecution();
    },
  };
}
