import { node } from "./dom.js";
/** @typedef {Record<string, any>} Wire */
/** @typedef {{generation:number, id:string, instance:string, token:string, controller:AbortController,
 * cancelled:boolean, cancelSent:boolean, probe:number, retired:boolean, cancelTask:Promise<void>|null}} Execution */

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
  root.classList.add("db-console");
  const status = node("p", STATES.checking);
  status.setAttribute("aria-live", "polite");
  const notice = node("p", "查询受保护的临时数据。SQL 的 WHERE／LIMIT 不缩小准备范围。结果有上限，不是数据库总量。");
  notice.className = "muted";
  const catalog = node("section");
  catalog.setAttribute("aria-label", "诊断对象目录");
  catalog.className = "db-catalog";
  const catalogIdentity = node("p");
  const availability = node("p", "可用性：核验中");
  const resourceSummary = node("p");
  const catalogDetails = document.createElement("details");
  const catalogSummary = node("summary", "对象目录");
  const objectList = node("div");
  catalogDetails.append(catalogSummary, objectList);
  const limitsDetails = document.createElement("details");
  const limitsBody = node("div");
  limitsDetails.append(node("summary", "完整限制与寿命"), limitsBody);
  catalog.append(catalogIdentity, availability, resourceSummary, catalogDetails, limitsDetails);
  const editor = node("textarea");
  editor.id = "database-sql";
  editor.setAttribute("aria-label", "SQL");
  editor.setAttribute("aria-describedby", "database-editor-help");
  editor.rows = 8;
  editor.spellcheck = false;
  editor.placeholder = "SELECT * FROM diag_sessions LIMIT 20";
  const editorLabel = node("label", "SQL 草稿");
  editorLabel.htmlFor = editor.id;
  const editorHelp = node("p", "Enter 换行。仅点击执行才提交；查询中可编辑下一份草稿。");
  editorHelp.id = "database-editor-help";
  editorHelp.className = "muted";
  const run = node("button", "执行");
  const cancel = node("button", "取消");
  const recheck = node("button", "核验查询与可用性");
  const cleanupText = node("p", "尚无查询清理责任");
  const bodyState = node("p", "尚未收到查询正文");
  const runReason = node("p");
  const protectionNotice = node("p");
  protectionNotice.className = "db-attention";
  const copySql = node("button", "复制 SQL");
  const copyResult = node("button", "复制本页结果");
  const sqlCopyFeedback = node("p");
  const resultCopyFeedback = node("p", "尚无有效结果可复制。");
  for (const feedback of [sqlCopyFeedback, resultCopyFeedback]) {
    feedback.className = "muted";
    feedback.setAttribute("role", "status");
  }
  const submitted = node("section");
  submitted.setAttribute("aria-label", "已提交查询");
  const draftRelation = node("p");
  draftRelation.className = "db-attention";
  const result = node("section");
  result.setAttribute("aria-label", "查询结果");
  const controls = node("div");
  controls.className = "db-actions";
  controls.append(run, cancel, recheck, copySql);
  const facts = node("section");
  facts.setAttribute("aria-label", "查询与资源状态");
  facts.className = "db-facts";
  for (const [title, value] of [["执行", status], ["正文", bodyState], ["清理", cleanupText]]) {
    const fact = node("div");
    fact.append(node("h3", /** @type {string} */ (title)), /** @type {HTMLElement} */ (value));
    facts.append(fact);
  }
  root.append(notice, catalog, editorLabel, editor, editorHelp, controls, sqlCopyFeedback,
    runReason, protectionNotice, facts, submitted, draftRelation, copyResult, resultCopyFeedback, result);

  let generation = 0;
  let pageGeneration = 0;
  let catalogBody = /** @type {Wire|null} */ (null);
  let execution = /** @type {Execution|null} */ (null);
  /** @type {Wire|null} */
  let resultMeta = null;
  let submittedSql = /** @type {string|null} */ (null);
  let inFlight = false;
  let cleanupVerified = true;
  let catalogVerified = false;
  let sqlDraft = editor.value;
  let rows = /** @type {Wire[][]} */ ([]);
  let columns = /** @type {string[]} */ ([]);
  let page = 0;
  let alive = true;
  let online = ctx.connected();
  let copyGeneration = 0;
  let catalogSignature = "";

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
    submittedSql = null;
    submitted.replaceChildren();
    draftRelation.textContent = "";
    result.replaceChildren();
    copyGeneration += 1;
    copyResult.disabled = true;
    resultCopyFeedback.textContent = "尚无有效结果可复制。";
    bodyState.textContent = "尚未收到查询正文";
  }

  function updateControls() {
    run.disabled = inFlight || !cleanupVerified || !catalogVerified || !online ||
      !ctx.connected() || !catalogBody?.available;
    cancel.disabled = !execution || execution.cancelled;
    copySql.disabled = !editor.value;
    copyResult.disabled = !resultMeta;
    recheck.disabled = !online || !ctx.connected();
    runReason.textContent = !online || !ctx.connected() ? "连接中断，核验恢复前不能执行。"
      : inFlight ? "当前查询尚在处理；可继续编辑下一份草稿。"
      : !cleanupVerified ? "查询清理尚未确认释放；请核验查询与可用性。"
      : !catalogVerified || !catalogBody?.available ? "数据库能力尚未确认可用；请重新核验。" : "";
  }

  /** @param {boolean} [preserveState] */
  async function loadCatalog(preserveState = false) {
    const mine = ++generation;
    catalogVerified = false;
    availability.textContent = "可用性：核验中";
    updateControls();
    if (!preserveState) setState("checking");
    try {
      const response = await fetch("/api/database", { cache: "no-store" });
      const body = await response.json();
      if (!alive || mine !== generation) return;
      if (!response.ok || body.instance_id !== ctx.instance()) {
        availability.textContent = "可用性：实例或能力未核验";
        if (body.instance_id && body.instance_id !== ctx.instance()) {
          retireExecution();
          clearResult();
          catalogBody = null;
        }
        setState("unavailable", "实例或能力未核验");
        return;
      }
      catalogBody = body;
      catalogVerified = true;
      availability.textContent = body.available ? "可用性：已核验可用" : `可用性：不可用 · ${body.reason || "原因未提供"}`;
      renderCatalog(body);
      if (!body.available) {
        if (!preserveState) setState("unavailable", body.reason || "");
        return;
      }
      if (!online || !ctx.connected()) {
        if (!preserveState) setState("unavailable", "连接中断");
        return;
      }
      if (!preserveState && cleanupVerified) setState("ready");
    } catch {
      if (alive && mine === generation) {
        availability.textContent = "可用性：网络失败，尚未核验";
        if (!preserveState) setState("unavailable", "网络失败");
      }
    } finally {
      if (alive && mine === generation) updateControls();
    }
  }

  /** @param {Wire} body */
  function renderCatalog(body) {
    catalogIdentity.textContent = `当前实例固定。记忆修订 ${body.memory_revision} · 保护规则版本 ${body.protection_version}`;
    const limits = body.limits || {};
    resourceSummary.textContent = `执行预算 ${limits.execute_budget_ms / 1000} 秒 · 最多 ${limits.result_rows} 行／${limits.result_columns} 列／${formatBytes(limits.result_utf8_bytes)} JSON`;
    // Catalog refresh changes facts, never the user's expanded objects or focus.
    const signature = JSON.stringify([body.objects, body.limits]);
    if (signature === catalogSignature) return;
    catalogSignature = signature;
    const opened = new Set(Array.from(objectList.querySelectorAll("details[open]"), item => item.getAttribute("data-object")));
    objectList.replaceChildren();
    for (const object of body.objects || []) {
      const card = document.createElement("details");
      card.dataset.object = object.name;
      card.open = opened.has(object.name);
      const heading = node("summary", object.name);
      const insert = node("button", "写入编辑器");
      insert.addEventListener("click", () => replaceDraft(object.example));
      const fields = node("dl");
      fields.className = "db-fields";
      for (const column of /** @type {Wire[]} */ (object.columns || [])) {
        fields.append(node("dt", column.name), node("dd",
          `${column.type} · ${column.nullable ? "可空" : "NOT NULL"} · ${column.identity ? "身份字段" : "非身份字段"}；来源 ${column.origin}；${column.protection}`));
      }
      const example = node("pre", object.example);
      example.tabIndex = 0;
      example.setAttribute("aria-label", `${object.name} 示例 SQL`);
      card.append(heading, node("p", `来源 ${object.source}`), node("p", object.notes || ""), fields, example, insert);
      objectList.append(card);
    }
    limitsBody.replaceChildren();
    const descriptions = [
      ["当前实例", String(body.instance_id)],
      ["SQL UTF-8", formatBytes(limits.sql_utf8_bytes)],
      ["源单值／源行", `${formatBytes(limits.source_value_bytes)}／${formatBytes(limits.source_row_bytes)}`],
      ["保护后输入", formatBytes(limits.protected_input_bytes)],
      ["SQLite heap", formatBytes(limits.sqlite_heap_bytes)],
      ["返回上限", `${limits.result_rows} 行／${limits.result_columns} 列／${formatBytes(limits.result_utf8_bytes)} JSON`],
      ["本地分页", `${limits.page_rows} 行；翻页不重发 SQL`],
      ["执行预算", `${limits.execute_budget_ms / 1000} 秒`],
      ["活动查询", "一次一个；句柄签发不等于执行获准"],
      ["句柄", `未执行 ${limits.handle_ttl_seconds} 秒到期；终态保留 ${limits.terminal_ttl_seconds} 秒；容量 ${limits.handle_capacity}`],
      ["取消／HTTP IO", "各 1 秒预算（既有服务合同，非目录 DTO 字段）"],
    ];
    const fields = node("dl");
    fields.className = "db-fields";
    for (const [label, value] of descriptions) fields.append(node("dt", label), node("dd", value));
    limitsBody.append(fields, node("p", "句柄期限不是结果 TTL。已显示结果没有固定倒计时；读取时刻持续可见，遗忘、保护变化、断连或真正离页会使其失效。"));
  }

  /** @param {number} bytes */
  function formatBytes(bytes) {
    if (!Number.isFinite(bytes)) return "未知";
    if (bytes >= 1024 * 1024) return `${bytes / (1024 * 1024)} MiB`;
    if (bytes >= 1024) return `${bytes / 1024} KiB`;
    return `${bytes} 字节`;
  }

  /** @param {string} sql */
  function replaceDraft(sql) {
    const apply = () => {editor.value = sql; sqlDraft = sql; updateDraftRelation(); updateControls();};
    if (!editor.value.trim() || editor.value === sql) {apply(); return;}
    const selection = [editor.selectionStart, editor.selectionEnd];
    const direction = editor.selectionDirection;
    const dialog = document.createElement("dialog");
    dialog.setAttribute("aria-label", "替换 SQL 草稿");
    const keep = node("button", "保留当前编辑");
    const replace = node("button", "放弃后替换");
    const finish = (discard = false) => {
      if (discard) apply();
      dialog.close();
      dialog.remove();
      editor.focus();
      if (!discard) editor.setSelectionRange(selection[0], selection[1], direction);
    };
    keep.onclick = () => finish();
    replace.onclick = () => finish(true);
    dialog.addEventListener("cancel", event => {event.preventDefault(); finish();});
    dialog.append(node("h2", "替换 SQL 草稿"),
      node("p", "示例会替换当前 SQL；不会执行查询或改变已有结果。"), keep, replace);
    root.append(dialog);
    dialog.showModal();
    keep.focus();
  }

  function resultDescription() {
    const start = page * 100;
    const bits = [rows.length ? `第 ${start + 1}–${Math.min(start + 100, rows.length)} 行` : "0 行",
      `本次返回 ${rows.length} 行（非源库总量）`];
    const reasons = /** @type {string[]} */ (resultMeta?.truncation_reasons || []);
    const reasonNames = reasons.map(reason => reason === "rows" ? "行数上限 (rows)" : reason === "bytes" ? "字节上限 (bytes)" : reason);
    bits.push(resultMeta?.truncated ? `结果截断：${reasonNames.join("、") || "原因未提供"}` : "返回完整：未截断");
    bits.push(`读取 ${resultMeta?.read_at || "时刻未提供"}`);
    bits.push(`查询 ${resultMeta?.query_id || "身份未提供"}`);
    const sources = resultMeta?.objects;
    bits.push(`来源 ${Array.isArray(sources) && sources.length ? sources.join("、") : "常量表达式（未引用诊断对象）"}`);
    bits.push(`保护 ${resultMeta?.protection_version || "未知"} · 记忆修订 ${resultMeta?.memory_revision ?? "未知"}`);
    const coverage = resultMeta?.coverage;
    bits.push(`覆盖 ${coverage?.notice || "未提供说明"}`);
    const attempts = coverage?.attempts;
    if (attempts && typeof attempts === "object") bits.push(
      `attempts recorded=${attempts.recorded ?? "未知"} empty=${attempts.recorded_empty ?? "未知"} unrecorded=${attempts.unrecorded ?? "未知"} in_progress=${attempts.in_progress_runs ?? "未知"}`);
    return bits;
  }

  function renderRows() {
    result.replaceChildren();
    copyGeneration += 1;
    copyResult.disabled = !resultMeta;
    if (!resultMeta) return;
    resultCopyFeedback.textContent = "仅复制当前页的列、值和结果说明；不会补查或导出数据库。";
    const meta = node("div");
    meta.className = "db-result-meta";
    for (const bit of resultDescription()) meta.append(node("p", bit));
    result.append(meta);
    if (!columns.length) return;
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const hr = document.createElement("tr");
    for (const name of columns) {
      const th = node("th", name);
      th.scope = "col";
      hr.append(th);
    }
    head.append(hr);
    table.append(head);
    const body = document.createElement("tbody");
    for (const row of rows.slice(page * 100, page * 100 + 100)) {
      const tr = document.createElement("tr");
      for (const cell of row) {
        const td = document.createElement("td");
        const value = node("span", cellText(cell));
        value.className = cell.type === "null" ? "db-cell sql-null" : "db-cell";
        if (cellText(cell).length > 160) {
          value.tabIndex = 0;
          value.setAttribute("role", "region");
          value.setAttribute("aria-label", "完整单元格值");
        }
        td.append(value);
        tr.append(td);
      }
      body.append(tr);
    }
    table.append(body);
    const scroll = node("div");
    scroll.className = "table-scroll";
    scroll.tabIndex = 0;
    scroll.setAttribute("role", "region");
    scroll.setAttribute("aria-label", "数据表横向滚动区");
    scroll.append(table);
    result.append(scroll);
    if (rows.length > 100) {
      const pagination = node("div");
      pagination.className = "db-actions";
      const prev = node("button", "上一页");
      const next = node("button", "下一页");
      prev.disabled = page === 0;
      next.disabled = (page + 1) * 100 >= rows.length;
      /** @param {number} delta */
      const move = (delta) => {
        page += delta;
        renderRows();
        // Pagination is explicit; keep keyboard focus on its replacement control.
        const buttons = result.querySelectorAll("button");
        const preferred = /** @type {HTMLButtonElement} */ (buttons[delta > 0 ? 1 : 0]);
        (preferred.disabled ? buttons[delta > 0 ? 0 : 1] : preferred)?.focus();
      };
      prev.onclick = () => move(-1);
      next.onclick = () => move(1);
      pagination.append(prev, next);
      result.append(pagination);
    }
  }

  /** @param {Wire} cell */
  function cellText(cell) {
    if (cell.type === "null") return "NULL";
    if (cell.type === "integer") return String(cell.value);
    if (cell.type === "real") return `${cell.value}${Number.isInteger(cell.value) ? ".0" : ""}`;
    if (cell.type === "blob") return `BLOB ${cell.hex} (${cell.byte_length} 字节)`;
    // Quoted text distinguishes empty text and literal "NULL" from SQL NULL.
    return JSON.stringify(cell.value ?? "");
  }

  /** @param {"sql"|"result"} kind */
  async function copy(kind) {
    if (!alive || (kind === "result" && !resultMeta)) return;
    const mine = copyGeneration;
    const feedback = kind === "sql" ? sqlCopyFeedback : resultCopyFeedback;
    const value = kind === "sql" ? editor.value : resultDescription().join("\n") + "\n\n" +
      columns.join("\t") + "\n" + rows.slice(page * 100, page * 100 + 100)
        .map(row => row.map(cellText).join("\t")).join("\n") + "\n";
    try {
      await navigator.clipboard.writeText(value);
      if (alive && (kind === "sql" || mine === copyGeneration)) feedback.textContent =
        kind === "sql" ? "已复制当前 SQL 草稿。" : "已复制本页结果与说明。";
    } catch {
      if (alive && (kind === "sql" || mine === copyGeneration)) feedback.textContent = "复制失败；请检查剪贴板权限后重试。";
    }
  }

  function updateDraftRelation() {
    draftRelation.className = submittedSql !== null && editor.value !== submittedSql ? "db-attention" : "muted";
    draftRelation.textContent = submittedSql === null ? "" : editor.value === submittedSql
      ? "当前草稿与本次提交相同。" : "当前草稿已改变；结果仍属于本次提交。";
  }

  /** @param {Execution} attempt */
  function ownsPage(attempt) {
    return alive && execution === attempt && attempt.generation === pageGeneration;
  }

  async function execute() {
    if (inFlight || !cleanupVerified || !catalogVerified || !online || !ctx.connected() || !catalogBody?.available) return;
    const token = ctx.csrf();
    if (!token) {
      catalogVerified = false;
      setState("unavailable", "入口凭据尚未就绪，请稍后核验。");
      updateControls();
      return;
    }
    const attempt = {
      generation: ++pageGeneration, id: "", instance: ctx.instance(), token,
      controller: new AbortController(), cancelled: false, cancelSent: false, probe: 0, retired: false, cancelTask: /** @type {Promise<void>|null} */ (null),
    };
    execution = attempt;
    const catalogAtStart = catalogBody;
    const sql = editor.value;
    inFlight = true;
    cleanupVerified = false;
    cleanupText.textContent = "清理尚未确认";
    bodyState.textContent = "等待查询正文";
    updateControls();
    clearResult();
    bodyState.textContent = "等待查询正文";
    submittedSql = sql;
    protectionNotice.textContent = "";
    const submittedDetails = document.createElement("details");
    const submittedText = node("pre", sql);
    submittedText.tabIndex = 0;
    submittedText.setAttribute("aria-label", "本次提交的完整 SQL");
    submittedDetails.append(node("summary", "查看本次提交的 SQL"), submittedText);
    submitted.append(node("h2", "已提交查询"), submittedDetails);
    updateDraftRelation();
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
          cleanupVerified = true; // No handle was issued, so this page owns no worker.
          cleanupText.textContent = "未签发查询句柄";
          await loadCatalog(true);
        }
        return;
      }
      attempt.id = handle.query_id;
      if (ownsPage(attempt) && !attempt.cancelled) submitted.prepend(node("p", `查询 ${attempt.id}`));
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
      inFlight = false;
      if (!executed.ok) {
        fail(payload);
        await probeMissing(attempt, true);
        return;
      }
      if (
        payload.instance_id !== catalogBody?.instance_id ||
        payload.memory_revision !== catalogBody?.memory_revision ||
        payload.protection_version !== catalogBody?.protection_version
      ) {
        setState("invalidated");
        clearResult();
        await probeMissing(attempt, true);
        return;
      }
      columns = payload.columns || [];
      rows = payload.rows || [];
      resultMeta = payload;
      bodyState.textContent = "已收到查询正文";
      page = 0;
      renderRows();
      if (payload.truncated) setState("truncated", /** @type {string[]} */ (payload.truncation_reasons || []).map(reason => reason === "rows" ? "行数上限" : reason === "bytes" ? "字节上限" : reason).join("、"));
      else if (!rows.length) setState("empty");
      else setState("complete");
      await probeMissing(attempt, true);
    } catch {
      if (!ownsPage(attempt)) return;
      inFlight = false;
      if (attempt.id) {
        if (!attempt.cancelled) await probeMissing(attempt);
      } else {
        // Issuance has settled without an ID, so this page never sent execute.
        // An unknown unused handle owns no worker; require a fresh capability
        // check, but do not wait for cleanup evidence that cannot be looked up.
        execution = null;
        cleanupVerified = true;
        catalogVerified = false;
        clearResult();
        availability.textContent = "可用性：需要重新核验";
        bodyState.textContent = "未发送执行请求";
        cleanupText.textContent = "未启动执行，无执行资源待清理。";
        setState("unavailable", "签发回执未收到；请核验后显式执行。");
      }
      updateControls();
    } finally {
      if (ownsPage(attempt)) {
        inFlight = false;
        updateControls();
      }
    }
  }

  /** @param {Execution} attempt @param {Wire} body @param {boolean} preserveOutcome */
  function showTerminal(attempt, body, preserveOutcome) {
    if (!ownsPage(attempt)) return;
    if (body.query_id && body.query_id !== attempt.id) return;
    if (body.instance_id && body.instance_id !== ctx.instance()) return;
    cleanupVerified = body.cleanup === "released" &&
      ["completed", "failed", "timed_out", "invalidated", "cancelled", "unused"].includes(body.status);
    cleanupText.textContent = body.cleanup === "failed" ? "清理失败"
      : body.cleanup === "released" ? "清理已释放"
      : body.cleanup === "pending" ? "清理中" : "清理尚未确认";
    if (!resultMeta) bodyState.textContent = attempt.cancelled ? "正文已清除；晚到响应不会恢复。" : "结果正文未收到";
    if (!online || attempt.retired || (preserveOutcome && body.status !== "invalidated")) {updateControls(); return;}
    switch (body.status) {
      case "completed":
        if (!resultMeta) setState(attempt.cancelled ? "complete" : "missing",
          attempt.cancelled ? "取消前已完成" : undefined);
        break;
      case "failed": setState("failed"); break;
      case "timed_out": setState("timeout"); break;
      case "invalidated": clearResult(); setState("invalidated"); break;
      case "cancelled": setState(cleanupVerified ? "stopped" : "cancelling"); break;
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
    updateControls();
  }

  /** @param {Execution} attempt @param {boolean} [preserveOutcome] */
  async function probeMissing(attempt, preserveOutcome = false) {
    const probe = ++attempt.probe;
    try {
      const response = await fetch(`/api/database/queries/${encodeURIComponent(attempt.id)}`, {
        cache: "no-store",
      });
      const body = await response.json();
      if (!ownsPage(attempt) || probe !== attempt.probe) return;
      showTerminal(attempt, body, preserveOutcome);
      if (cleanupVerified) await loadCatalog(!attempt.retired);
    } catch {
      if (ownsPage(attempt) && probe === attempt.probe) {
        cleanupVerified = false;
        cleanupText.textContent = "清理尚未确认";
        if (!preserveOutcome) setState("unavailable", "网络失败");
        updateControls();
      }
    }
  }

  /** @param {Execution} attempt @param {boolean} [keepalive] */
  function cancelHandle(attempt, keepalive = false) {
    if (!attempt.id) return Promise.resolve();
    if (attempt.cancelTask) return attempt.cancelTask;
    attempt.cancelSent = true;
    const probe = ++attempt.probe;
    attempt.cancelTask = (async () => {
      try {
        const response = await fetch(`/api/database/queries/${encodeURIComponent(attempt.id)}/cancel`, {
          method: "POST", cache: "no-store", keepalive,
          headers: {"Content-Type": "application/json", "x-agent-alfred-csrf": attempt.token},
          body: "{}",
        });
        const body = await response.json();
        if (probe !== attempt.probe) return;
        showTerminal(attempt, body, false);
        if (ownsPage(attempt) && cleanupVerified && online) await loadCatalog(!attempt.retired);
      } catch {
        if (ownsPage(attempt) && online) await probeMissing(attempt);
      }
    })();
    return attempt.cancelTask;
  }

  /** @param {Execution} attempt */
  async function recheckRetired(attempt) {
    attempt.generation = pageGeneration;
    await cancelHandle(attempt, true);
    if (ownsPage(attempt) && online) await probeMissing(attempt, true);
  }

  /** @param {Wire} body */
  function fail(body) {
    const code = body?.code;
    if (code === "database_busy") setState("busy");
    else if (code === "query_timeout") setState("timeout");
    else if (code === "query_cancelled") setState("stopped");
    else if (code === "data_invalidated") setState("invalidated");
    else if (code === "cleanup_failed") {setState("failed"); cleanupText.textContent = "清理失败";}
    else if (code === "sql_error" || code === "sql_rejected") setState("sql_error", body.detail);
    else if (code === "database_unavailable") setState("unavailable", body.detail);
    else if (code === "invalid_request") setState("请求被拒绝", "SQL 需在 64 KiB 内、返回列不超过 64，且请求格式须有效。");
    else if (code === "input_too_large") setState("输入超过上限", "完整准备失败，没有部分结果。");
    else if (code === "result_too_large") setState("结果超过字节上限", "没有可返回的完整行。");
    else if (code === "resource_limit") setState("诊断资源不足", "请核验能力后再试。");
    else setState("sql_error", body?.detail || code || "执行错误");
    clearResult();
  }

  function requestCancel() {
    const attempt = execution;
    if (!attempt || !ownsPage(attempt)) return;
    attempt.cancelled = true;
    attempt.probe += 1;
    cleanupVerified = false;
    clearResult();
    cleanupText.textContent = "清理尚未确认";
    bodyState.textContent = "正文已清除；晚到响应不会恢复。";
    setState("cancelling");
    updateControls();
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
      attempt.retired = true;
      attempt.probe += 1;
      cleanupVerified = false;
      attempt.controller.abort();
      void cancelHandle(attempt, true);
    }
  }

  run.addEventListener("click", () => void execute());
  cancel.addEventListener("click", () => void requestCancel());
  recheck.addEventListener("click", () => {
    if (execution?.id) void probeMissing(execution, !!resultMeta);
    else if (!inFlight) void loadCatalog();
  });
  copySql.addEventListener("click", () => void copy("sql"));
  copyResult.addEventListener("click", () => void copy("result"));
  editor.addEventListener("input", () => {
    sqlDraft = editor.value;
    updateDraftRelation();
    sqlCopyFeedback.textContent = "";
    updateControls();
  });

  void loadCatalog();

  return {
    getLeaveState() {return {dirty:editor.value.trim()!=="", summary:"手写 SQL 尚未保存；离开会清空编辑器和受管结果。", pending:inFlight || !cleanupVerified};},
    sync() {
      if (!alive || online) return;
      online = true;
      if (execution && execution.instance !== ctx.instance()) {
        // A new authoritative process owns a different admission domain. Do
        // not apply its missing-handle response to the old process's cleanup.
        retireExecution();
        execution = null;
        cleanupVerified = true;
        clearResult();
        cleanupText.textContent = "旧实例查询已失效；旧实例清理未由此页核验。";
        protectionNotice.textContent = "实例已更换：已清除受管结果；SQL 草稿保留，不会自动重跑。";
        void loadCatalog();
      } else if (execution?.id && !cleanupVerified) void probeMissing(execution);
      else void loadCatalog();
    },
    /** @param {string} kind */
    invalidate(kind) {
      if (kind === "protection" || kind === "memory") {
        generation += 1;
        catalogVerified = false;
        retireExecution();
        clearResult();
        editor.value = sqlDraft;
        setState("invalidated");
        protectionNotice.textContent = "记忆或保护版本变化：已清除结果及提交关联；草稿保留，核验后仍需显式执行。";
        if (execution?.id) void recheckRetired(execution);
        else {cleanupVerified = true; void loadCatalog();}
      }
    },
    disconnect() {
      online = false;
      catalogVerified = false;
      generation += 1;
      run.disabled = true;
      retireExecution();
      clearResult();
      editor.value = sqlDraft;
      setState("unavailable", "连接中断");
      if (execution?.id) execution.generation = pageGeneration;
      updateControls();
    },
    suspend() {
      generation += 1;
      retireExecution();
      run.disabled = true;
      catalogVerified = false;
      catalogBody = null;
      clearResult();
      editor.value = sqlDraft = "";
    },
    restoredFromCache() {
      retireExecution();
      clearResult();
      editor.value = "";
      sqlDraft = "";
      if (execution?.id) void recheckRetired(execution);
      else {cleanupVerified = true; void loadCatalog();}
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
