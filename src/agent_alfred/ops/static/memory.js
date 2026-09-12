import { node } from "./dom.js";
/** @typedef {Record<string, any>} Wire */
/**
 * @typedef {{state:"ok", body:Wire}
 *   | {state:"failed", status:number, code:string, body:Wire|null}
 *   | {state:"offline"} | {state:"stale"}} ReadResult
 */
/**
 * `unconfirmed`: no response or a server-side failure, so the write may or
 * may not have happened and is resent with the same operation id.
 * @typedef {{ok:boolean, unconfirmed:boolean, status:number, code:string|null, body:Wire|null}} PostResult
 */

const KIND_LABEL = /** @type {Record<string,string>} */ ({
  semantic: "语义记忆",
  episodic: "情景记忆",
});
const HISTORY_NOTE =
  "原始会话与删除前的追踪仍可人工查看，不在追溯擦除范围内。";

/**
 * One safe code from the three existing response shapes: #31 `error.code`,
 * #18 action `result.error.code`, and transport/guard top-level `code`.
 * @param {number} status @param {Wire|null} body
 */
function failureCode(status, body) {
  return (
    body?.error?.code || body?.result?.error?.code || body?.code || `http_${status}`
  );
}

/**
 * The page's view of the persisted memory revision. Bodies are shown only
 * while `state` is "online": the stream is connected and the revision was
 * read after connecting. A read started before the latest invalidation, or
 * answering an older revision, is discarded and never rendered.
 */
export class MemorySync {
  constructor() {
    this.instance = "";
    this.revision = -1;
    /** @type {"offline"|"verifying"|"online"|"unverified"} */ this.state = "offline";
    // Bumped by every invalidation; reads started under an older value are stale.
    this.invalidations = 0;
    // Bumped by every (re)connection; an older verification cannot finish.
    this.connections = 0;
    /** @type {Set<()=>void>} */ this.listeners = new Set();
  }
  get online() {
    return this.state === "online";
  }
  /** @param {()=>void} listener */
  watch(listener) {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }
  /** @param {"offline"|"verifying"|"online"|"unverified"} state */
  enter(state) {
    this.state = state;
    this.invalidations++;
    for (const listener of [...this.listeners]) listener();
  }
  /** A (re)connected stream: read the persisted revision before any body. @param {string} instance */
  async connected(instance) {
    this.instance = instance;
    const connection = ++this.connections;
    this.enter("verifying");
    try {
      const response = await fetch("/api/memory/state");
      const body = await response.json();
      if (connection !== this.connections) return;
      if (!response.ok || body.process_instance_id !== instance)
        throw new Error("memory state unavailable");
      this.revision = Math.max(this.revision, body.memory_revision);
      this.enter("online");
    } catch {
      if (connection === this.connections) this.enter("unverified");
    }
  }
  disconnected() {
    this.connections++;
    this.enter("offline");
  }
  /** Body-free invalidation; frames while disconnected, from another process or not advancing are ignored. @param {Wire} body */
  patch(body) {
    if (
      this.state === "offline" ||
      this.state === "unverified" ||
      body.process_instance_id !== this.instance ||
      !(body.memory_revision > this.revision)
    )
      return;
    this.revision = body.memory_revision;
    if (this.online) this.enter("online");
    else this.invalidations++;
  }
  /**
   * A revision-bound read; a `memory_changed` race is read at most three times.
   * @param {string} path @param {Record<string,string>} params
   * @returns {Promise<ReadResult>}
   */
  async read(path, params) {
    for (let attempt = 0; attempt < 3; attempt++) {
      if (!this.online) return { state: "offline" };
      const invalidations = this.invalidations;
      /** @type {Response} */ let response;
      /** @type {Wire|null} */ let body;
      try {
        response = await fetch(path + "?" + new URLSearchParams(params));
        body = await response.json();
      } catch {
        return invalidations === this.invalidations
          ? { state: "failed", status: 0, code: "network", body: null }
          : { state: "stale" };
      }
      if (invalidations !== this.invalidations || !this.online)
        return { state: "stale" };
      if (response.status === 409 && failureCode(409, body) === "memory_changed")
        continue;
      const revision = body?.memory_revision ?? body?.error?.memory_revision;
      if (typeof revision === "number") {
        if (revision < this.revision) return { state: "stale" };
        if (revision > this.revision) {
          // A write we were not told about yet: everything older is stale.
          this.revision = revision;
          queueMicrotask(() => {
            if (this.online) this.enter("online");
          });
          this.invalidations++;
        }
      }
      return response.ok && body
        ? { state: "ok", body }
        : {
            state: "failed",
            status: response.status,
            code: failureCode(response.status, body),
            body,
          };
    }
    return { state: "failed", status: 409, code: "memory_changed", body: null };
  }
}

/** @param {string} path @param {Wire} body @param {string} token @returns {Promise<PostResult>} */
async function post(path, body, token) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-agent-alfred-csrf": token,
      },
      body: JSON.stringify(body),
    });
  } catch {
    return { ok: false, unconfirmed: true, status: 0, code: "network", body: null };
  }
  /** @type {Wire|null} */ let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  // A success status is not a receipt. An interrupted or truncated body leaves
  // the write committed-or-not, so it stays unconfirmed under the same id
  // rather than borrowing `response.ok` as a result.
  if (response.ok && (payload === null || typeof payload !== "object"))
    return {
      ok: false,
      unconfirmed: true,
      status: response.status,
      code: "malformed_response",
      body: null,
    };
  return {
    ok: response.ok,
    // A 503 naming a `reason` is a refusal before any write, e.g. a trace
    // barrier that could not be confirmed: definite, not unconfirmed.
    unconfirmed: response.status >= 500 && !payload?.error?.reason,
    status: response.status,
    code: response.ok
      ? null
      : payload?.error?.reason || failureCode(response.status, payload),
    body: payload,
  };
}

/**
 * The complete receipt a command answer must carry. A success status with a
 * partial body confirms nothing: without these fields the operation keeps its
 * unconfirmed state, its draft, and its same-id query or resend.
 * @param {Wire|null|undefined} body @param {Wire} operation
 */
function commandReceipt(body, operation) {
  const result = body?.result;
  const statuses = /** @type {Record<string,string[]>} */ ({
    save: ["saved", "already_exists"],
    update: ["updated", "unchanged"],
    delete: ["deleted", "already_absent"],
  });
  if (body?.schema_version !== 1 || body.operation_id !== operation.operation_id ||
      result?.operation_id !== operation.operation_id || result?.action !== operation.action ||
      result?.kind !== operation.kind || !statuses[operation.action]?.includes(result?.status) ||
      typeof result?.memory_id !== "string" || !result.memory_id ||
      typeof result.committed_at !== "string" || !Number.isFinite(Date.parse(result.committed_at)) ||
      ![0, 1].includes(result.affected_count)) return null;
  if (operation.action === "delete" ? result.record_version !== null
      : !Number.isInteger(result.record_version) || result.record_version < 1) return null;
  const requestedId = operation.request?.payload?.id;
  if (requestedId !== undefined && result.memory_id !== requestedId) return null;
  return body;
}

/** @param {Wire|null|undefined} origin */
export function originLabel(origin) {
  if (origin?.type === "manual")
    return `用户手填（${origin.source === "cli" ? "CLI" : "Web"}）`;
  if (origin?.type === "tool") return `工具写入 · 调用 ${origin.call_id}`;
  if (origin?.type === "consolidation") return `记忆提炼 · 批次 ${origin.batch_id}`;
  return "来源未知";
}

/** @param {Wire} record */
export function interval(record) {
  if (record.occurred_until === null) return `瞬时 · ${record.occurred_at}`;
  const range = `[${record.occurred_at}, ${record.occurred_until})`;
  return record.occurred_until === record.occurred_at ? `${range} · 空区间` : range;
}

/** A record's known source groups as Run links, or why there are none. @param {Wire|undefined} provenance */
export function sourceGroups(provenance) {
  const groups = provenance?.source_groups || [];
  const source = node(
    "p",
    groups.length
      ? "来源组："
      : provenance?.state === "known_none"
        ? "来源组：无历史来源（手动录入）"
        : "来源组：不可追溯（历史关联未知）",
  );
  for (const group of groups) {
    const link = node("a", `Run ${group}`);
    link.href = `/runs/${encodeURIComponent(group)}`;
    source.append(" ", link);
  }
  return source;
}

/** @param {number} value */
function pad(value) {
  return String(value).padStart(2, "0");
}
/** A datetime-local value, read as this browser's local wall time, made aware. @param {string} value */
function awareLocal(value) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return null;
  const minutes = -date.getTimezoneOffset();
  const size = Math.abs(minutes);
  const offset = `${minutes < 0 ? "-" : "+"}${pad(Math.floor(size / 60))}:${pad(size % 60)}`;
  return `${value.length === 16 ? value + ":00" : value}${offset}`;
}
/** @param {string} iso */
function localInput(iso) {
  const date = new Date(iso);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** @template {HTMLElement} T @param {HTMLElement} form @param {string} label @param {T} control @returns {T} */
function field(form, label, control) {
  const wrapper = node("label", label);
  wrapper.append(control);
  form.append(wrapper);
  return control;
}
/** @param {string} type */
function input(type = "text") {
  const result = node("input");
  result.type = type;
  if (type === "datetime-local") result.step = "1";
  return result;
}

const RESULT_LABEL = /** @type {Record<string,(r:Wire)=>string>} */ ({
  saved: (r) => `已保存 · ID ${r.memory_id} · 版本 ${r.record_version}`,
  already_exists: (r) =>
    `已存在同一条记忆（机械幂等，未新增） · ID ${r.memory_id} · 版本 ${r.record_version}`,
  updated: (r) => `已更新 · ID ${r.memory_id} · 版本 ${r.record_version}`,
  unchanged: (r) => `内容无变化 · ID ${r.memory_id} · 版本 ${r.record_version}`,
  deleted: (r) => `记忆已删除（数据库已确认） · ID ${r.memory_id}`,
  already_absent: (r) => `记录此前已不存在 · ID ${r.memory_id}`,
});
const ACTION_LABEL = /** @type {Record<string,string>} */ ({
  save: "保存",
  update: "修改",
  delete: "删除",
});
const REFUSAL = /** @type {Record<string,string>} */ ({
  busy: "宿主正忙（运行或其他写入进行中）：本次未执行，也不会排队；请稍后手动重试。",
  invalid_input: "输入不合法：请检查必填项与时间格式。",
  not_found: "记录已不存在。",
  operation_mismatch: "该操作标识已用于不同内容，未重复执行。",
  operation_unverifiable: "旧操作无法核验，未重新执行。",
  trace_barrier_failed: "追踪屏障未确认：删除尚未执行，可稍后重试。",
});

const RECEIPTS_KEY = "alfred.memory.operations";
// One list per tab: every Memory page instance shares it, so an older page's
// late completion cannot overwrite a newer page's receipts.
/** @type {Wire[]} */ const receiptEntries = [];
try {
  receiptEntries.push(...JSON.parse(sessionStorage.getItem(RECEIPTS_KEY) || "[]"));
} catch {
  /* An unreadable store starts empty; nothing pending can be claimed. */
}
// A reload during a send lost the response: that result is unconfirmed.
for (const entry of receiptEntries)
  if (entry.state === "sending") {
    entry.state = "pending";
    entry.code = "interrupted";
  }

/**
 * Receipts are committed facts; a missing response is "unconfirmed", not a
 * failure. Only an unconfirmed command keeps its request, so it can be resent
 * with the same operation id; deletes keep polling forgetting progress.
 * @param {{sync:MemorySync, csrf:()=>string, changed:()=>void, confirmed:(entry:Wire)=>void}} page
 */
function receiptsPanel(page) {
  const section = node("section");
  section.setAttribute("aria-label", "记忆操作回执");
  const entries = receiptEntries;
  // Unsubmitted UI choices are scoped to an exact operation and scope revision.
  /** @type {Map<string, Map<string, number>>} */ const selections = new Map();
  /** @type {Map<string, number>} */ const confirmations = new Map();
  /** @type {Map<string,string>} */ const notes = new Map();
  // Coalesce concurrent reads, but remember an invalidation during a read.
  /** @type {Map<string,boolean>} */ const querying = new Map();
  function persist() {
    // Bound settled history, never the only recovery path for pending work.
    let settled = 0;
    for (let i = entries.length - 1; i >= 0; i--) {
      const entry = entries[i];
      if (entry.state !== "committed" ||
          (entry.result?.status === "deleted" && entry.forgetting?.state !== "complete"))
        continue;
      if (++settled > 20) entries.splice(i, 1);
    }
    sessionStorage.setItem(RECEIPTS_KEY, JSON.stringify(entries));
  }
  /** @param {Wire} entry @param {Wire} body */
  function committed(entry, body) {
    entry.state = "committed";
    entry.result = body.result;
    entry.forgetting = body.forgetting ?? null;
    entry.scopes = body.scopes ?? null;
    delete entry.request;
  }
  /** @param {Wire} request @returns {Promise<PostResult>} */
  async function submit(request) {
    let entry = entries.find((item) => item.operation_id === request.operation_id);
    if (!entry) {
      entry = { operation_id: request.operation_id, action: request.action, kind: request.kind };
      entries.push(entry);
    }
    entry.state = "sending";
    entry.request = request;
    notes.delete(entry.operation_id);
    // A reload can interrupt any response, even before fetch settles.
    persist();
    render();
    let response = await post("/api/memory/commands", request, page.csrf());
    if (response.ok && !commandReceipt(response.body, entry))
      response = {
        ok: false,
        unconfirmed: true,
        status: response.status,
        code: "malformed_response",
        body: null,
      };
    if (entry.state === "committed") {
      // A concurrent read-back may already have confirmed this send. A late
      // transport failure cannot revoke that fact or restore its request.
    } else if (response.ok && response.body) {
      committed(entry, response.body);
      // The receipt carries progress only; scope snapshots come from the
      // operation read, which the invalidation may have raced ahead of.
      if (entry.result?.status === "deleted" && entry.forgetting?.state !== "complete")
        void query(entry);
    } else if (response.unconfirmed) {
      entry.state = "pending";
      entry.code = response.code;
    } else if (entries.includes(entry)) entries.splice(entries.indexOf(entry), 1);
    persist();
    render();
    // #18 D07: a lost response reads the persisted fact first. An invalidation
    // that arrived while this command was sending could not reach it.
    if (entry.state === "pending") void query(entry);
    return response;
  }
  /** @param {Wire} entry */
  async function query(entry) {
    if (querying.has(entry.operation_id)) {
      querying.set(entry.operation_id, true);
      return;
    }
    querying.set(entry.operation_id, false);
    const unconfirmed = entry.state !== "committed";
    const confirmation = confirmations.get(entry.operation_id) || 0;
    const obsolete = () => confirmation !== (confirmations.get(entry.operation_id) || 0);
    try {
      const response = await fetch(
        "/api/memory/operations?" +
          new URLSearchParams({ operation_id: entry.operation_id }),
      );
      const body = await response.json();
      if (obsolete()) return;
      if (response.ok && commandReceipt(body, entry)) {
        committed(entry, body);
        notes.delete(entry.operation_id);
        // Only a newly confirmed command can have changed what panels show.
        if (unconfirmed) {
          page.confirmed(entry);
          page.changed();
        }
      } else {
        // A never-committed update has no receipt to resolve. Its explicit
        // record id still identifies the projection; only a revision-bound
        // not-found fact may remove its reusable body, never a read failure.
        const id = entry.action === "update" ? entry.request?.payload?.id : null;
        if (unconfirmed && response.status === 404 && typeof id === "string") {
          const current = await page.sync.read("/api/memory/record", { kind: entry.kind, id });
          if (current.state === "failed" && current.status === 404 && current.code === "not_found")
            discard(entry.operation_id);
        }
        const code = response.ok
          ? "malformed_response"
          : failureCode(response.status, body);
        notes.set(
          entry.operation_id,
          !unconfirmed
            ? `清理进度暂不可读取（${code}）。`
            : response.status === 404
              ? "尚未找到提交记录：仍未确认，可重新发送同一操作。"
              : `查询失败（${code}）：仍未确认。`,
        );
      }
    } catch {
      if (obsolete()) return;
      notes.set(
        entry.operation_id,
        unconfirmed ? "查询失败：仍未确认。" : "清理进度暂不可读取。",
      );
    } finally {
      const again = querying.get(entry.operation_id);
      querying.delete(entry.operation_id);
      // One follow-up only for an actual coalesced request. A local complete
      // receipt cannot cancel requested verification of current progress.
      if (again) void query(entry);
    }
    persist();
    render();
  }
  /**
   * Every invalidation reaches the unconfirmed commands too: a lost response
   * left a reusable command projection behind, and ADR-0007 has it follow the
   * record it turns out to have written rather than wait to be asked.
   */
  async function refresh() {
    for (const entry of entries)
      if (
        entry.state === "pending" || entry.state === "sending" ||
        (entry.state === "committed" && entry.action === "delete")
      )
        await query(entry);
  }

  /**
   * A pending command whose record the page has seen forgotten drops the body
   * it kept for a resend; its identity and same-id query stay.
   * @param {string} operationId
   */
  function discard(operationId) {
    const entry = entries.find((item) => item.operation_id === operationId);
    if (!entry || entry.state === "committed" || !entry.request) return;
    delete entry.request;
    persist();
    render();
  }
  /** @param {Wire} entry @param {Wire} action */
  async function forget(entry, action) {
    let response = await post(
      "/api/memory/forget/actions",
      { schema_version: 1, operation_id: entry.operation_id, ...action },
      page.csrf(),
    );
    if (response.ok && !response.body?.forgetting)
      response = {
        ok: false,
        unconfirmed: true,
        status: response.status,
        code: "malformed_response",
        body: null,
      };
    if (response.ok && response.body) {
      confirmations.set(entry.operation_id, (confirmations.get(entry.operation_id) || 0) + 1);
      entry.forgetting = response.body.forgetting;
      delete entry.confirming;
      notes.delete(entry.operation_id);
      persist();
      render();
      await query(entry);
      return;
    }
    notes.set(
      entry.operation_id,
      response.code === "scope_stale"
        ? "范围已变化：请查看最新范围后重新确认。"
        : response.unconfirmed
          ? `结果未确认（${response.code}）：可用同一确认重新发送。`
          : REFUSAL[response.code || ""] || `未执行（${response.code}）。`,
    );
    if (response.code === "scope_stale") {
      delete entry.confirming;
      await query(entry);
    } else render();
  }
  /** @param {HTMLElement} row @param {Wire} entry */
  function progress(row, entry) {
    const state = entry.forgetting?.state;
    const labels = /** @type {Record<string,string>} */ ({
      cleaning: "受管副本清理中；数据库删除已确认。",
      failed: "记忆已删除，副本清理未完成。",
      needs_scope: "来源关联不完整：确认隔离范围之前遗忘尚未完成。",
      complete: "遗忘完成：条目、索引与受管副本均已清理并核实。",
    });
    row.append(
      node(
        "p",
        entry.forgetting?.error
          ? "清理进度暂不可读取。"
          : labels[state] || "清理进度未知。",
      ),
    );
    for (const item of entry.forgetting?.cleanup || [])
      if (item.state !== "complete")
        row.append(
          node("small", `${item.target_id} · ${item.state}${item.error ? " · " + item.error : ""} `),
        );
    if (state === "failed") {
      const retry = node("button", "重试清理");
      retry.addEventListener("click", () =>
        void forget(entry, { action: "retry_cleanup" }),
      );
      row.append(retry);
    }
    const pending = (Array.isArray(entry.scopes) ? entry.scopes : []).filter(
      (scope) => scope.state === "pending",
    );
    const selected = selections.get(entry.operation_id) || new Map();
    selections.set(entry.operation_id, selected);
    for (const [id, revision] of selected)
      if (state !== "needs_scope" || !pending.some(scope => scope.scope_id === id && scope.revision === revision))
        selected.delete(id);
    if (state === "needs_scope" && pending.length) {
      const form = node("fieldset");
      form.append(
        node("legend", "待确认的历史范围（服务端快照）"),
        node("p", "确认后这些历史组不再进入自动上下文；原始会话仍可人工查看。"),
      );
      /** @type {HTMLInputElement[]} */ const boxes = [];
      for (const scope of pending) {
        const box = input("checkbox");
        box.value = scope.scope_id;
        box.checked = selected.get(scope.scope_id) === scope.revision;
        box.addEventListener("change", () => {
          if (box.checked) selected.set(scope.scope_id, scope.revision);
          else selected.delete(scope.scope_id);
        });
        boxes.push(box);
        const bounds = scope.boundary || {};
        field(
          form,
          `${scope.kind} ${scope.container_id ?? "未知容器"} · ${bounds.member_count ?? scope.members.length} 个历史组 · ${bounds.earliest_at ?? "时间未知"} 至 ${bounds.latest_at ?? "时间未知"} · 修订 ${scope.revision} `,
          box,
        );
      }
      const confirm = node("button", "确认隔离所选范围");
      confirm.addEventListener("click", () => {
        const chosen = pending
          .filter((_scope, index) => boxes[index].checked)
          .map((scope) => ({
            scope_id: scope.scope_id,
            expected_revision: scope.revision,
          }));
        if (!chosen.length) return;
        entry.confirming = { action_id: crypto.randomUUID(), scopes: chosen };
        persist();
        void forget(entry, { action: "resolve_scope", ...entry.confirming });
      });
      form.append(confirm);
      row.append(form);
    }
    if (entry.confirming) {
      const resend = node("button", "重新发送同一确认");
      resend.addEventListener("click", () =>
        void forget(entry, { action: "resolve_scope", ...entry.confirming }),
      );
      row.append(resend);
    }
    row.append(node("small", HISTORY_NOTE));
  }
  function render() {
    section.replaceChildren(node("h2", "操作回执"));
    if (!entries.length) section.append(node("p", "本标签页尚无记忆操作。"));
    for (const entry of [...entries].reverse()) {
      const row = node("article");
      row.className = "card";
      row.dataset.operation = entry.operation_id;
      const title = `${ACTION_LABEL[entry.action] || entry.action} · ${KIND_LABEL[entry.kind] || entry.kind} · 操作 ${entry.operation_id}`;
      row.append(node("strong", title));
      if (entry.state === "sending") row.append(node("p", "正在提交…"));
      if (entry.state === "pending") {
        row.append(
          node(
            "p",
            `结果待确认（${entry.code}）：无回执不等于失败，可能已提交也可能未提交。`,
          ),
        );
        const ask = node("button", "查询结果");
        ask.addEventListener("click", () => void query(entry));
        row.append(ask);
        if (entry.request) {
          const resend = node("button", "重新发送同一操作");
          resend.addEventListener("click", () => void submit(entry.request));
          row.append(resend);
        } else
          row.append(node("p", "该记录已删除：可复用正文已清除，仅保留同 ID 查询。"));
      }
      if (entry.state === "committed") {
        const label = RESULT_LABEL[entry.result?.status];
        row.append(
          node(
            "p",
            `${label ? label(entry.result) : entry.result?.status} · 提交于 ${entry.result?.committed_at}`,
          ),
        );
        if (entry.result?.status === "deleted") progress(row, entry);
      }
      const note = notes.get(entry.operation_id);
      if (note) row.append(node("p", note));
      section.append(row);
    }
  }
  render();
  return { section, submit, refresh, render, discard };
}

/**
 * The shell owns command recovery for the whole tab, including a reload on
 * /runs. The sole receipt list keeps bodies only until a verified receipt;
 * page callbacks are detached on navigation and never own recovery.
 * @param {MemorySync} sync @param {()=>string} csrf
 */
export function memoryReceipts(sync, csrf) {
  /** @type {{changed:()=>void, confirmed:(entry:Wire)=>void}|null} */
  let view = null;
  const receipts = receiptsPanel({
    sync,
    csrf,
    changed: () => view?.changed(),
    confirmed: (entry) => view?.confirmed(entry),
  });
  sync.watch(() => { if (sync.online) void receipts.refresh(); });
  return {
    ...receipts,
    /** @param {{changed:()=>void, confirmed:(entry:Wire)=>void}} callbacks */
    attach(callbacks) { view = callbacks; },
    detach() { view = null; },
  };
}

/**
 * @param {"semantic"|"episodic"} kind
 * @param {{sync:MemorySync, submit:(request:Wire)=>Promise<PostResult>,
 *   openBatch:(id:string)=>void, discard:(operationId:string)=>void}} page
 */
function recordPanel(kind, page) {
  const semantic = kind === "semantic";
  const label = KIND_LABEL[kind];
  const panel = node("div");
  const searchForm = node("form");
  searchForm.setAttribute("aria-label", `搜索${label}`);
  const text = field(searchForm, "文本", input());
  const subject = semantic ? field(searchForm, "主题（精确匹配）", input()) : null;
  const since = semantic ? null : field(searchForm, "起点（包含）", input("datetime-local"));
  const until = semantic ? null : field(searchForm, "终点（不含）", input("datetime-local"));
  const bounds = node("small");
  const searchButton = node("button", "搜索");
  searchButton.type = "submit";
  const showAll = node("button", "显示全部");
  showAll.type = "button";
  searchForm.append(bounds, searchButton, showAll);
  const status = node("p");
  status.setAttribute("role", "status");
  const list = node("div");
  list.setAttribute("aria-label", `${label}列表`);
  const more = node("button", "加载更多");
  const reread = node("button", "从首页重新读取");
  const detail = node("section");
  detail.setAttribute("aria-label", `${label}详情`);
  const editForm = node("form");
  editForm.setAttribute("aria-label", `编辑${label}`);
  const editSubject = semantic ? field(editForm, "主题", input()) : null;
  const editBody = field(editForm, semantic ? "事实" : "摘要", node("textarea"));
  const editStart = semantic ? null : field(editForm, "发生起点", input("datetime-local"));
  const editEnd = semantic ? null : field(editForm, "发生终点（留空表示瞬时）", input("datetime-local"));
  const editNotice = node("p");
  editNotice.setAttribute("role", "status");
  const saveEdit = node("button", "保存修改");
  saveEdit.type = "submit";
  const cancelEdit = node("button", "放弃草稿");
  cancelEdit.type = "button";
  editForm.append(editNotice, saveEdit, cancelEdit);
  editForm.hidden = true;
  const createForm = node("form");
  createForm.setAttribute("aria-label", `新增${label}`);
  const newSubject = semantic ? field(createForm, "新主题", input()) : null;
  const newBody = field(createForm, semantic ? "新事实" : "新摘要", node("textarea"));
  const newStart = semantic ? null : field(createForm, "新发生起点", input("datetime-local"));
  const newEnd = semantic ? null : field(createForm, "新发生终点（留空表示瞬时）", input("datetime-local"));
  const createNotice = node("p");
  createNotice.setAttribute("role", "status");
  const createButton = node("button", "保存");
  createButton.type = "submit";
  createForm.append(createButton);
  if (!semantic)
    createForm.prepend(node("p", "时间按本机时区解释并带 offset 提交；终点不含。"));
  // Show the exact aware instants that will be submitted, offset included.
  for (const [start, end] of [[newStart, newEnd], [editStart, editEnd]]) {
    if (!start || !end) continue;
    const preview = node("small");
    const show = () => {
      const from = awareLocal(start.value);
      const to = awareLocal(end.value);
      preview.textContent = from
        ? `将提交 ${to ? `[${from}, ${to})` : `瞬时 ${from}`}`
        : "尚未填写发生起点。";
    };
    start.addEventListener("input", show);
    end.addEventListener("input", show);
    start.form?.addEventListener("reset", () => setTimeout(show));
    end.parentElement?.after(preview);
    show();
  }
  panel.append(
    node("h2", label),
    searchForm,
    status,
    list,
    more,
    reread,
    detail,
    editForm,
    node("h3", `新增${label}`),
    createNotice,
    createForm,
  );

  /** @type {Record<string,string>} */ let filters = {};
  /** @type {Wire[]} */ let records = [];
  let cursor = "";
  let listState = "idle";
  let listCode = "";
  let listRequest = 0;
  /** @type {{id:string, record:Wire|null, state:string, code?:string}|null} */ let selected = null;
  let detailRequest = 0;
  let detailMessage = "";
  // The version the user chose to delete; a changed record needs a new choice.
  /** @type {number|null} */ let confirming = null;
  /** @type {{id:string, base:number, original:Record<string,string>}|null} */ let editing = null;
  // Resending an unconfirmed submit of an unchanged draft is the same user
  // action, so it keeps its operation id; any change starts a new action.
  /** @type {{operation_id:string, request:string}|null} */ let editAttempt = null;
  /** @type {{operation_id:string, request:string, memory_id?:string}|null} */
  let createAttempt = null;
  let createDraftVerified = -1;
  /** @type {{operation_id:string, request:string}|null} */ let deleteAttempt = null;
  /** @param {{operation_id:string, request:string}|null} attempt @param {Wire} request */
  function operationFor(attempt, request) {
    const text = JSON.stringify(request);
    return {
      operation_id: attempt?.request === text ? attempt.operation_id : crypto.randomUUID(),
      request: text,
    };
  }

  /** The save request the current new-record draft would submit. */
  function createRequest() {
    /** @type {Wire} */ const payload = semantic
      ? { subject: newSubject?.value || "", fact: newBody.value }
      : {
          summary: newBody.value,
          occurred_at: awareLocal(newStart?.value || ""),
          occurred_until: newEnd?.value ? awareLocal(newEnd.value) : null,
        };
    return { kind, action: "save", payload };
  }

  function boundsText() {
    if (semantic || !since || !until) return;
    const start = awareLocal(since.value);
    const end = awareLocal(until.value);
    bounds.textContent =
      start || end
        ? `将查询 [${start ?? "无下界"}, ${end ?? "无上界"})：起点包含、终点不含。`
        : "起点包含、终点不含；时间按本机时区解释。";
  }
  since?.addEventListener("input", boundsText);
  until?.addEventListener("input", boundsText);
  boundsText();

  /** @param {boolean} [append] */
  async function load(append = false) {
    const token = ++listRequest;
    if (!append) {
      records = [];
      cursor = "";
    }
    listState = "loading";
    renderList();
    const result = await page.sync.read("/api/memory/records", {
      kind,
      page_size: "25",
      ...filters,
      ...(append && cursor ? { cursor } : {}),
    });
    if (token !== listRequest || result.state === "stale") return;
    if (result.state === "offline") {
      listState = "offline";
    } else if (result.state === "failed") {
      listCode = result.code;
      listState = result.code === "cursor_stale" ? "stale_cursor" : "failed";
      // A stale page is never spliced; a failed refresh never shows old data.
      if (!append || listState === "stale_cursor") records = [];
    } else {
      records = [...(append ? records : []), ...result.body.records];
      cursor = result.body.next_cursor || "";
      listState = "ready";
    }
    renderList();
  }
  function renderList() {
    createForm.hidden = Boolean(createAttempt) &&
      (!page.sync.online || createDraftVerified !== page.sync.invalidations);
    list.replaceChildren();
    more.hidden = !(listState === "ready" && cursor && page.sync.online);
    reread.hidden = !["failed", "stale_cursor"].includes(listState);
    if (!page.sync.online) {
      status.textContent =
        page.sync.state === "verifying"
          ? "正在核验记忆状态…"
          : "连接中断：记忆正文已隐藏，重连并核验后再显示。";
      return;
    }
    status.textContent =
      listState === "loading"
        ? "正在读取…"
        : listState === "failed"
          ? `读取失败（${listCode}）：不能确认当前内容，未显示旧列表。`
          : listState === "stale_cursor"
            ? "列表已变化，分页游标已失效：请从首页重新读取。"
            : listState === "ready" && !records.length
              ? filters.mode === "search"
                ? "没有匹配的记忆。"
                : "记忆库为空。"
              : "";
    for (const record of records) {
      const card = node("article");
      card.className = "card";
      card.dataset.memoryId = record.id;
      if (semantic) card.append(node("h3", record.subject), node("p", record.fact));
      else card.append(node("p", record.summary), node("p", interval(record)));
      card.append(node("small", meta(record)));
      const open = node("button", "查看详情");
      open.addEventListener("click", () => void openRecord(record.id));
      card.append(open);
      list.append(card);
    }
  }
  /** @param {Wire} record */
  function meta(record) {
    return `版本 ${record.record_version} · 创建：${originLabel(record.origin)} · ${record.created_at} · 最近修改：${originLabel(record.last_change_origin)} · ${record.modified_at} · ${record.human_protected ? "人工保护" : "未受人工保护"}`;
  }
  /** @param {string} id */
  async function openRecord(id) {
    if (selected?.id !== id) {
      confirming = null;
      detailMessage = editing && editing.id !== id ? "已切换记录，未提交的编辑草稿已放弃。" : "";
      if (editing && editing.id !== id) clearDraft();
    }
    selected = { id, record: null, state: "loading" };
    renderDetail();
    await verify();
  }
  async function verify() {
    if (!selected) return;
    const id = selected.id;
    const token = ++detailRequest;
    const result = await page.sync.read("/api/memory/record", { kind, id });
    if (token !== detailRequest || selected?.id !== id || result.state === "stale") return;
    if (result.state === "offline") selected.state = "loading";
    else if (result.state === "failed" && result.status === 404) {
      selected = { id, record: null, state: "gone" };
      confirming = null;
      if (editing?.id === id) {
        // R10: the draft goes, and so does the copy an unanswered update left
        // in the receipts panel to resend.
        if (editAttempt) page.discard(editAttempt.operation_id);
        clearDraft();
        detailMessage = "记录已不存在，编辑草稿已清除。";
      }
    } else if (result.state === "failed") {
      // A read failure is not absence: the draft stays and nothing is shown as current.
      selected = { id, record: null, state: "failed", code: result.code };
    } else selected = { id, record: result.body.record, state: "ready" };
    renderDetail();
  }
  function clearDraft() {
    editing = null;
    editAttempt = null;
    editForm.reset();
    editNotice.textContent = "";
  }
  function renderDetail() {
    detail.replaceChildren();
    editForm.hidden = true;
    if (detailMessage) detail.append(node("p", detailMessage));
    if (!selected) return;
    detail.append(node("h3", "记录详情"));
    if (!page.sync.online) {
      detail.append(node("p", "连接中断：正文已隐藏，重连并核验后再显示。"));
      return;
    }
    if (selected.state === "loading") {
      detail.append(node("p", "正在读取当前内容…"));
      return;
    }
    if (selected.state === "gone") {
      detail.append(node("p", "记录已不存在（可能已被删除）；不显示旧正文。"));
      return;
    }
    if (selected.state !== "ready" || !selected.record) {
      const retry = node("button", "重新读取");
      retry.addEventListener("click", () => void verify());
      detail.append(
        node("p", `读取失败（${selected.code}）：不能确认记录是否存在，也不作为不存在处理。`),
        retry,
      );
      editForm.hidden = !editing;
      return;
    }
    const record = selected.record;
    if (semantic) detail.append(node("h4", record.subject), node("p", record.fact));
    else detail.append(node("p", record.summary), node("p", interval(record)));
    detail.append(node("p", meta(record)));
    detail.append(sourceGroups(record.provenance));
    if (record.origin?.type === "consolidation") {
      const batch = node("button", "查看提炼批次");
      batch.addEventListener("click", () => page.openBatch(record.origin.batch_id));
      detail.append(batch);
    }
    const edit = node("button", "编辑");
    edit.addEventListener("click", () => startEdit(record));
    const remove = node("button", "删除");
    remove.addEventListener("click", () => {
      confirming = record.record_version;
      renderDetail();
    });
    detail.append(edit, remove);
    if (confirming === record.record_version) {
      const box = node("div");
      box.setAttribute("role", "group");
      box.setAttribute("aria-label", "删除确认");
      const confirm = node("button", "确认删除");
      confirm.addEventListener("click", () => void removeRecord(record));
      const cancel = node("button", "取消");
      cancel.addEventListener("click", () => {
        confirming = null;
        renderDetail();
      });
      box.append(
        node(
          "p",
          `确认删除版本 ${record.record_version}？条目与索引会真删，受管副本随后清理；${HISTORY_NOTE}`,
        ),
        confirm,
        cancel,
      );
      detail.append(box);
    }
    if (editing && editing.id === record.id) {
      editForm.hidden = false;
      if (editing.base !== record.record_version && !editNotice.textContent)
        editNotice.textContent = `当前记录已是版本 ${record.record_version}，草稿基于版本 ${editing.base}；直接提交会产生版本冲突。`;
    }
  }
  /** @param {Wire} record */
  function startEdit(record) {
    if (editSubject) editSubject.value = record.subject;
    editBody.value = semantic ? record.fact : record.summary;
    if (editStart) editStart.value = localInput(record.occurred_at);
    if (editEnd)
      editEnd.value =
        record.occurred_until === null ? "" : localInput(record.occurred_until);
    editStart?.dispatchEvent(new Event("input"));
    // Compare later edits with what the controls normalized (for example a
    // zero seconds field), so an untouched time is never resubmitted.
    const original = {
      subject: editSubject?.value ?? "",
      body: editBody.value,
      start: editStart?.value ?? "",
      end: editEnd?.value ?? "",
    };
    editing = { id: record.id, base: record.record_version, original };
    editNotice.textContent = "";
    confirming = null;
    renderDetail();
    editBody.focus();
  }
  editForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void submitEdit();
  });
  cancelEdit.addEventListener("click", () => {
    clearDraft();
    renderDetail();
  });
  async function submitEdit() {
    if (!editing) return;
    const draft = editing;
    const original = draft.original;
    /** @type {Wire} */ const payload = { id: draft.id };
    if (editSubject && editSubject.value !== original.subject)
      payload.subject = editSubject.value;
    if (editBody.value !== original.body)
      payload[semantic ? "fact" : "summary"] = editBody.value;
    if (editStart && editStart.value !== original.start)
      payload.occurred_at = awareLocal(editStart.value);
    if (editEnd && editEnd.value !== original.end)
      payload.occurred_until = editEnd.value ? awareLocal(editEnd.value) : null;
    const request = { kind, action: "update", payload, expected_version: draft.base };
    editAttempt = operationFor(editAttempt, request);
    saveEdit.disabled = true;
    const response = await page.submit({
      schema_version: 1,
      operation_id: editAttempt.operation_id,
      ...request,
    });
    saveEdit.disabled = false;
    if (editing !== draft) return;
    if (!response.unconfirmed) editAttempt = null;
    if (response.ok) {
      const result = response.body?.result;
      clearDraft();
      detailMessage =
        result?.status === "updated"
          ? `已更新到版本 ${result.record_version}。`
          : `内容无变化（版本 ${result?.record_version}）。`;
      void verify();
      return;
    }
    const error = response.body?.error || {};
    if (response.code === "not_found") {
      clearDraft();
      detailMessage = "记录已被删除，编辑草稿已清除。";
      void verify();
      return;
    }
    if (response.code === "version_conflict") {
      editNotice.replaceChildren(
        `版本冲突：记录当前为版本 ${error.current_version}，草稿基于版本 ${draft.base}；草稿已保留，未覆盖新版本。`,
      );
      const rebase = node("button", "基于当前版本继续编辑");
      rebase.type = "button";
      rebase.addEventListener("click", () => {
        if (editing !== draft) return;
        draft.base = error.current_version;
        editNotice.textContent = `草稿现基于版本 ${draft.base}，请核对上方当前内容后再保存。`;
        void verify();
      });
      editNotice.append(rebase);
      void verify();
      return;
    }
    editNotice.textContent =
      response.code === "duplicate_conflict"
        ? `与已有记忆 ${error.existing_id} 内容相同，未合并；草稿已保留。`
        : response.unconfirmed
          ? "结果待确认：草稿已保留，可在操作回执中查询或重新发送同一操作。"
          : `${REFUSAL[response.code || ""] || `未保存（${response.code}）。`}草稿已保留。`;
  }
  /** @param {Wire} record */
  async function removeRecord(record) {
    confirming = null;
    renderDetail();
    const request = {
      kind,
      action: "delete",
      payload: { id: record.id },
      expected_version: record.record_version,
    };
    deleteAttempt = operationFor(deleteAttempt, request);
    const response = await page.submit({
      schema_version: 1,
      operation_id: deleteAttempt.operation_id,
      ...request,
    });
    if (!response.unconfirmed) deleteAttempt = null;
    if (selected?.id !== record.id) return;
    if (response.ok) {
      if (editing?.id === record.id) clearDraft();
      selected = { id: record.id, record: null, state: "gone" };
      detailMessage =
        response.body?.result?.status === "deleted"
          ? "记忆已删除；遗忘进度见操作回执。"
          : "记录此前已不存在。";
      renderDetail();
      return;
    }
    detailMessage =
      response.code === "version_conflict"
        ? `记录已被修改（当前版本 ${response.body?.error?.current_version}），未删除；请查看当前内容后再确认。`
        : response.unconfirmed
          ? "删除结果待确认：可在操作回执中查询或重新发送同一操作。"
          : REFUSAL[response.code || ""] || `未删除（${response.code}）。`;
    void verify();
  }
  createForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void create();
  });
  async function create() {
    const request = createRequest();
    createAttempt = operationFor(createAttempt, request);
    createDraftVerified = page.sync.invalidations;
    createButton.disabled = true;
    const response = await page.submit({
      schema_version: 1,
      operation_id: createAttempt.operation_id,
      ...request,
    });
    createButton.disabled = false;
    if (!response.unconfirmed) createAttempt = null;
    if (response.ok) {
      const result = response.body?.result;
      createForm.reset();
      createForm.hidden = false;
      createNotice.textContent =
        result?.status === "saved"
          ? `已保存（ID ${result.memory_id}，版本 ${result.record_version}）。`
          : `已存在同一条记忆（ID ${result?.memory_id}），未新增。`;
      return;
    }
    createNotice.textContent =
      response.unconfirmed
        ? "结果待确认：草稿已保留，可在操作回执中查询或重新发送同一操作。"
        : `${REFUSAL[response.code || ""] || `未保存（${response.code}）。`}草稿已保留。`;
  }
  /**
   * A command the receipts panel confirmed after its response was lost. The
   * new-record draft it was sent from is a copy of that record's body from
   * here on, so it follows the record instead of outliving it. An edit draft
   * already follows the open record through `verify`.
   * @param {Wire} entry
   */
  function confirmed(entry) {
    const attempt = createAttempt;
    if (
      entry.kind !== kind ||
      entry.action !== "save" ||
      !attempt ||
      attempt.operation_id !== entry.operation_id ||
      attempt.request !== JSON.stringify(createRequest())
    )
      return;
    createAttempt = { ...attempt, memory_id: entry.result.memory_id };
    void checkCreateDraft();
  }
  /**
   * Clear the draft a confirmed save wrote once that record is gone. Only an
   * answered 404 is absence: an offline, stale or failed read keeps the draft,
   * and a later invalidation asks again.
   */
  async function checkCreateDraft() {
    const attempt = createAttempt;
    const id = attempt?.memory_id;
    if (!id || attempt.request !== JSON.stringify(createRequest())) return;
    const result = await page.sync.read("/api/memory/record", { kind, id });
    if (createAttempt !== attempt || attempt.request !== JSON.stringify(createRequest())) return;
    if (result.state === "ok") {
      createDraftVerified = page.sync.invalidations;
      createForm.hidden = false;
      return;
    }
    if (result.state !== "failed" || result.status !== 404) return;
    createForm.reset();
    createForm.hidden = false;
    createAttempt = null;
    createNotice.textContent = `此前未回执的保存已确认（ID ${id}），该记忆随后已被删除：草稿正文已清除。`;
  }

  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    /** @type {Record<string,string>} */ const next = { mode: "search" };
    if (text.value) next.text = text.value;
    if (subject?.value) next.subject = subject.value;
    const start = since?.value ? awareLocal(since.value) : null;
    const end = until?.value ? awareLocal(until.value) : null;
    if (start) next.since = start;
    if (end) next.until = end;
    filters = next;
    void load();
  });
  showAll.addEventListener("click", () => {
    searchForm.reset();
    boundsText();
    filters = {};
    void load();
  });
  more.addEventListener("click", () => void load(true));
  reread.addEventListener("click", () => void load());
  return {
    panel,
    confirmed,
    refresh() {
      // Any invalidation hides the open body until the current record is re-read.
      if (selected) selected = { id: selected.id, record: null, state: "loading" };
      renderList();
      renderDetail();
      if (!page.sync.online) return;
      void load();
      void verify();
      void checkCreateDraft();
    },
  };
}

/** @param {HTMLElement} root */
function skillsPanel(root) {
  const list = node("div");
  list.setAttribute("aria-label", "Skill 列表");
  root.append(
    node("h2", "Skill 目录"),
    node(
      "p",
      "只读目录：启动时建立索引，文件修改需重启后生效。当前版本不做 Skill 匹配或注入，这里也不提供编辑。",
    ),
    list,
  );
  let loaded = false;
  async function load() {
    if (loaded) return;
    loaded = true;
    try {
      const response = await fetch("/api/memory/skills");
      const body = await response.json();
      if (!response.ok) throw new Error(failureCode(response.status, body));
      if (!body.skills.length) list.append(node("p", "没有可用的 Skill。"));
      for (const skill of body.skills) {
        const card = node("article");
        card.className = "card";
        card.append(
          node("h3", skill.name),
          node("p", skill.description),
          node(
            "small",
            `${skill.source === "user" ? "用户 Skill" : "内置 Skill"}${skill.overrides_builtin ? " · 已整体覆盖同名内置 Skill" : ""}`,
          ),
        );
        const open = node("button", "查看正文");
        const text = node("pre");
        open.addEventListener("click", async () => {
          try {
            const reply = await fetch(
              "/api/memory/skill?" + new URLSearchParams({ name: skill.name }),
            );
            const loadedSkill = await reply.json();
            if (!reply.ok) throw new Error(failureCode(reply.status, loadedSkill));
            text.textContent = loadedSkill.skill.body;
          } catch {
            text.textContent = "正文暂不可读取。";
          }
        });
        card.append(open, text);
        list.append(card);
      }
    } catch (failure) {
      list.replaceChildren(
        node(
          "p",
          `Skill 目录不可用（${failure instanceof Error ? failure.message : "unknown"}）：配置或读取故障，不是空目录。`,
        ),
      );
    }
  }
  return { load };
}

/** @param {Wire} ratio @param {string} label */
function ratioText(ratio, label) {
  return `${label} ${ratio.numerator}/${ratio.denominator}${ratio.ratio === null ? "（无数据）" : ` = ${(ratio.ratio * 100).toFixed(1)}%`}`;
}

/** @param {()=>string|null} session */
function statisticsPanel(session) {
  const section = node("section");
  section.setAttribute("aria-label", "检索统计");
  const scoped = input("checkbox");
  const refresh = node("button", "刷新统计");
  const output = node("div");
  section.append(node("h2", "检索统计"));
  field(section, "仅当前会话", scoped);
  section.append(refresh, output);
  async function load() {
    const target = session();
    scoped.disabled = target === null;
    /** @type {Record<string,string>} */
    const params = scoped.checked && target !== null ? { session_id: target } : {};
    try {
      const response = await fetch(
        "/api/memory/statistics?" + new URLSearchParams(params),
      );
      const body = await response.json();
      if (!response.ok) throw new Error(failureCode(response.status, body));
      const stats = body.statistics;
      const excluded = stats.excluded;
      output.replaceChildren(
        node(
          "p",
          `范围 [${stats.since}, ${stats.until}) · ${stats.session_id === null ? "全部会话" : "当前会话"}的已记录对话运行`,
        ),
        node("p", ratioText(stats.skip, "跳过 S/(S+H+M)")),
        node("p", ratioText(stats.hit, "命中 H/(H+M)")),
        node("p", ratioText(stats.error, "错误 E/(S+H+M+E)")),
        node(
          "p",
          `不进入比例：未记录 ${excluded.unrecorded} · 未评估 ${excluded.not_evaluated} · 不完整 ${excluded.incomplete} · 旧或不支持 ${excluded.legacy_unknown}`,
        ),
      );
    } catch (failure) {
      output.replaceChildren(
        node("p", `统计暂不可读取（${failure instanceof Error ? failure.message : ""}）。`),
      );
    }
  }
  scoped.addEventListener("change", () => void load());
  refresh.addEventListener("click", () => void load());
  return { section, load };
}

const BATCH_LABEL = /** @type {Record<string,string>} */ ({
  queued: "排队中",
  running: "提炼中",
  awaiting_approval: "待批准（覆盖人工保护记忆）",
  succeeded: "已提交",
  rejected: "已拒绝",
  failed: "失败",
  invalidated: "已失效：依赖记录或来源已变化，候选正文已清除",
});
const ACTION_REFUSAL = /** @type {Record<string,string>} */ ({
  busy: "宿主正忙：操作未执行，也不会排队；请稍后手动重试。",
  batch_invalidated: "批次已失效：候选已清除，旧批准不能执行。",
  stale_revision: "批次已变化：请重新查看后再操作。",
  invalid_batch_state: "批次状态已改变，此操作不再适用。",
  source_mismatch: "来源已变化：请重新查看后再操作。",
  external_conflict: "镜像文件被外部修改：请确认重新生成。",
  stale_confirmation: "镜像文件又发生了变化：旧确认不能授权新的文件版本。",
  mirror_sync_failed: "镜像同步失败：数据库中的记忆不受影响，可再次同步重试。",
  storage_write_failed: "写入失败：本次操作未完成，可再次尝试。",
});

/** Validate the small #18 action reply, including its durable failure shape.
 * @param {Wire|null|undefined} body @param {Wire} action @param {number} status */
function actionResult(body, action, status) {
  const result = body?.result;
  if (body?.schema_version !== 1 || body.operation_id !== action.operation_id ||
      !result || typeof result !== "object" || Array.isArray(result)) return false;
  // Mirror failures are stored as {error:{code}} without a status or inner ID.
  if (result.error !== undefined)
    return typeof result.error?.code === "string" && Boolean(result.error.code);
  if (action.action.startsWith("mirror_"))
    return result.operation_id === action.operation_id &&
      (["pending", "interrupted"].includes(result.status) ||
        (result.status === "regenerated" && typeof result.target === "string"));
  if (action.action === "skip_oversized")
    return result.status === "skipped" && result.session_id === action.session_id &&
      result.run_id === action.run_id;
  if (result.batch_id !== action.batch_id ||
      !["succeeded", "rejected", "failed", "invalidated", "running", "awaiting_approval",
        "generation_required", "below_threshold", "source_too_large"].includes(result.status))
    return false;
  if (status === 202 || result.status === "running")
    return typeof result.generation_run_id === "string" && Boolean(result.generation_run_id);
  return Number.isInteger(result.revision) && result.revision >= 1;
}

const ACTIONS_KEY = "alfred.memory.actions";
// #18 D09: an unconfirmed queue or mirror action outlives the page that sent
// it. Only its identity, the versions it was authorized against and the note
// to show are kept -- never a candidate body or a mirror preview.
/** @type {Wire[]} */ const actionEntries = [];
/** One live view per action endpoint; replacing a page retires its renderer.
 * @type {Map<string,()=>void>} */ const actionViews = new Map();
try {
  actionEntries.push(...JSON.parse(sessionStorage.getItem(ACTIONS_KEY) || "[]"));
} catch {
  /* An unreadable store starts empty; nothing pending can be claimed. */
}

/**
 * #18 queue and mirror actions. A lost or failed response keeps the same
 * operation, which can be queried or resent unchanged; only a definite
 * answer clears it, so a response loss never turns into a second action.
 * @param {{note:HTMLElement, readPath:string, csrf:()=>string,
 *   describe:(result:Wire|undefined, status:number)=>string, settled:()=>void}} options
 */
function actionRunner({ note, readPath, csrf, describe, settled }) {
  let message = "";
  /** @param {string} key */
  function stored(key) {
    return actionEntries.find((item) => item.path === readPath && item.key === key);
  }
  /** A response belongs to an operation, not merely the button it came from.
   * @param {string} key @param {Wire} action */
  function current(key, action) {
    return stored(key)?.action.operation_id === action.operation_id;
  }
  function persist() {
    sessionStorage.setItem(ACTIONS_KEY, JSON.stringify(actionEntries));
    for (const update of actionViews.values()) update();
  }
  /** @param {string} key @param {Wire} action @param {string} text */
  function remember(key, action, text) {
    const entry = stored(key);
    if (entry) entry.note = text;
    else actionEntries.push({ path: readPath, key, action, note: text });
    persist();
  }
  /** A definite answer retires only this operation. @param {string} key @param {Wire} action */
  function settle(key, action) {
    if (!current(key, action)) return;
    actionEntries.splice(actionEntries.indexOf(/** @type {Wire} */ (stored(key))), 1);
    persist();
  }
  function render() {
    note.replaceChildren();
    if (message) note.append(node("p", message));
    for (const entry of actionEntries.filter((item) => item.path === readPath)) {
      const row = node("div");
      row.append(node("small", `操作 ${entry.action.operation_id}`), node("p", entry.note));
      const ask = node("button", "查询结果");
      ask.addEventListener("click", () => void query(entry.key));
      const resend = node("button", "重新发送同一操作");
      resend.addEventListener("click", () => void act(entry.action, entry.key));
      row.append(ask, resend);
      note.append(row);
    }
  }
  const update = () => {
    if (!note.isConnected) actionViews.delete(readPath);
    else render();
  };
  actionViews.set(readPath, update);
  /** @param {string} key */
  async function query(key) {
    const action = stored(key)?.action;
    if (!action) return;
    try {
      const response = await fetch(
        readPath + "?" + new URLSearchParams({ operation_id: action.operation_id }),
      );
      const body = await response.json();
      if (!current(key, action)) return;
      if (!response.ok) throw new Error(failureCode(response.status, body));
      if (!actionResult(body, action, 200)) throw new Error("malformed_response");
      if (body.result.status === "pending") {
        remember(key, action, "服务端操作仍待确认，可稍后查询或重送同一操作。");
        return;
      }
      message = body.result.error
        ? ACTION_REFUSAL[body.result.error.code] || `未执行（${body.result.error.code}）。`
        : describe(body.result, 200);
      settle(key, action);
      settled();
    } catch (failure) {
      if (!current(key, action)) return;
      remember(key, action,
        failure instanceof Error && failure.message === "not_found"
          ? "尚未找到提交记录：仍未确认，可重新发送同一操作。"
          : "查询失败：仍未确认。",
      );
    }
  }
  /** @param {Wire} body @param {string} key */
  async function act(body, key) {
    const action = stored(key)?.action || {
      ...body,
      schema_version: 1,
      operation_id: crypto.randomUUID(),
    };
    message = "";
    // Persist before dispatch: both reloads and page changes may lose a reply.
    remember(key, action, "正在提交…结果待确认，无回执不等于失败。");
    const response = await post("/api/memory/consolidation/actions", action, csrf());
    if (!current(key, action)) return;
    // A 5xx carrying a durable result is definite, so another click is new.
    const valid = actionResult(response.body, action, response.status);
    if ((response.ok || response.unconfirmed) && !valid) {
      const code = response.ok || response.body ? "malformed_response" : response.code;
      remember(key, action, `结果待确认（${code}）：无回执不等于失败。`);
      return;
    }
    if (valid && response.body?.result.status === "pending") {
      remember(key, action, "服务端操作仍待确认，可稍后查询或重送同一操作。");
      return;
    }
    const error = response.body?.result?.error?.code || response.code;
    message = error
      ? ACTION_REFUSAL[error] || `未执行（${error}）。`
      : describe(response.body?.result, response.status);
    settle(key, action);
    settled();
  }
  // Restore every operation, preserving its original versions and confirmation.
  // Only query automatically; a resend still goes through the server's guards.
  render();
  for (const entry of actionEntries.filter((item) => item.path === readPath))
    void query(entry.key);
  return act;
}

/**
 * Consolidation queue and managed mirrors, both from #18's service contract.
 * Candidate bodies and mirror previews are revision-bound like records.
 * @param {{sync:MemorySync, csrf:()=>string}} page
 */
function queuePanel(page) {
  const section = node("section");
  section.setAttribute("aria-label", "提炼队列");
  const output = node("div");
  const note = node("div");
  note.setAttribute("role", "status");
  const readError = node("p");
  let offset = 0;
  let sessionId = "";
  const filter = node("form");
  const sessionInput = field(filter, "按会话 ID 筛选队列", input());
  const apply = node("button", "筛选队列");
  apply.type = "submit";
  filter.append(apply);
  const previous = node("button", "上一页");
  const next = node("button", "下一页");
  const position = node("span");
  const navigation = node("div");
  navigation.append(previous, position, next);
  /** @param {number} nextOffset */
  function move(nextOffset) {
    offset = nextOffset;
    candidates.clear();
    void load();
  }
  previous.addEventListener("click", () => move(Math.max(0, offset - 50)));
  next.addEventListener("click", () => move(offset + 50));
  filter.addEventListener("submit", (event) => {
    event.preventDefault();
    // Historical Session IDs are opaque: even whitespace is identity.
    // Only a truly empty input removes this optional filter.
    sessionId = sessionInput.value;
    move(0);
  });
  section.append(
    node("h2", "提炼队列"),
    node(
      "p",
      "达到阈值后，于正常聊天保存后的空闲机会自动启动；不提供绕过阈值的立即提炼。",
    ),
    filter,
    navigation,
    note,
    readError,
    output,
  );
  /** @type {Map<string, Wire|string>} */ const candidates = new Map();
  let request = 0;
  let queue = /** @type {Wire|null} */ (null);
  async function load() {
    const token = ++request;
    queue = null;
    for (const id of candidates.keys()) candidates.set(id, "正在重新核验候选…");
    render();
    const result = await page.sync.read("/api/memory/consolidation", {
      limit: "50", offset: String(offset), ...(sessionId ? { session_id: sessionId } : {}),
    });
    if (token !== request || result.state === "stale") return;
    queue = result.state === "ok" ? result.body : null;
    readError.textContent =
      result.state === "failed" ? `队列暂不可读取（${result.code}）。` : "";
    render();
    for (const id of candidates.keys()) void candidate(id);
  }
  /** @param {string} id */
  async function candidate(id) {
    const token = request;
    candidates.set(id, "正在读取候选…");
    render();
    const result = await page.sync.read("/api/memory/consolidation", { batch_id: id });
    if (token !== request || result.state === "stale" || !candidates.has(id)) return;
    candidates.set(
      id,
      result.state === "ok" ? result.body.batch : `候选暂不可读取（${result.state === "failed" ? result.code : "连接中断"}）。`,
    );
    render();
  }
  const act = actionRunner({
    note,
    readPath: "/api/memory/consolidation",
    csrf: page.csrf,
    describe: (result, status) =>
      status === 202
        ? `已受理新的提炼运行（Run ${result?.generation_run_id ?? "未知"}）：它会再次调用主模型并可能产生费用，结果待确认。`
        : `已完成：${result?.status}${result?.generation_run_id ? `（提炼运行 ${result.generation_run_id}）` : ""}。`,
    settled: () => void load(),
  });
  function render() {
    output.replaceChildren();
    previous.disabled = !page.sync.online || !queue || offset === 0;
    next.disabled = !page.sync.online || !queue || !(queue.sessions_has_more || queue.batches_has_more);
    position.textContent = `第 ${offset / 50 + 1} 页（每页最多 50 个会话与 50 个批次）`;

    if (!page.sync.online) {
      output.append(node("p", "连接中断：候选正文已隐藏，重连并核验后再显示。"));
      return;
    }
    if (!queue) return;
    for (const session of queue.sessions) {
      const row = node("article");
      row.className = "card";
      row.append(
        node("strong", `会话 ${session.session_id}`),
        node(
          "p",
          session.error
            ? `状态暂不可读取（${session.error.code}）。`
            : `有效未处理 ${session.unprocessed_count} / 阈值 ${session.threshold}`,
        ),
      );
      if (session.block) {
        row.append(
          node(
            "p",
            `来源超限：Run ${session.block.run_id} 共 ${session.block.characters} 字符，上限 ${session.block.limit}；该会话提炼已停止。跳过不会提炼、也不删除原聊天。`,
          ),
        );
        const skip = node("button", "跳过此来源");
        skip.addEventListener("click", () =>
          void act(
            { action: "skip_oversized", session_id: session.session_id, run_id: session.block.run_id },
            `skip:${session.session_id}:${session.block.run_id}`,
          ),
        );
        row.append(skip);
      }
      output.append(row);
    }
    if (!queue.batches.length) output.append(node("p", "暂无提炼批次。"));
    // A batch opened from a record's origin may be older than the listed page.
    const listed = new Set(queue.batches.map((/** @type {Wire} */ batch) => batch.batch_id));
    for (const [id, detail] of candidates)
      if (!listed.has(id) && typeof detail === "string") {
        const row = node("article");
        row.className = "card";
        row.dataset.batch = id;
        row.append(node("strong", `批次 ${id}`), node("p", detail));
        output.append(row);
      }
    const opened = [...candidates.values()].filter(
      (detail) => typeof detail === "object" && !listed.has(detail.batch_id),
    );
    for (const batch of [...queue.batches, ...opened]) {
      const row = node("article");
      row.className = "card";
      row.dataset.batch = batch.batch_id;
      row.append(
        node("strong", `批次 ${batch.batch_id} · 修订 ${batch.revision}`),
        node("p", BATCH_LABEL[batch.status] || batch.status),
        node(
          "small",
          `来源对话 [${batch.source_range?.start ?? "未知"}, ${batch.source_range?.end ?? "未知"}] · 创建 ${batch.created_at} · 更新 ${batch.updated_at}${batch.finished_at ? " · 结束 " + batch.finished_at : ""}${batch.error_code ? " · 错误 " + batch.error_code : ""}`,
        ),
      );
      if (batch.generation_run_id) {
        const link = node("a", `提炼运行 ${batch.generation_run_id}`);
        link.href = `/runs/${encodeURIComponent(batch.generation_run_id)}?filter=system`;
        row.append(link);
      }
      if (batch.model) row.append(node("p", `模型 ${batch.model.endpoint_id} / ${batch.model.model_id}`));
      for (const usage of batch.model_usage || [])
        row.append(
          node(
            "small",
            `Attempt ${usage.attempt_id} · ${usage.outcome} · 输入 ${usage.usage?.total_input_tokens ?? "未知"} · 输出 ${usage.usage?.output_tokens ?? "未知"} `,
          ),
        );
      /** @param {string} action @param {string} text */
      const button = (action, text) => {
        const control = node("button", text);
        control.addEventListener("click", () =>
          void act(
            { action, batch_id: batch.batch_id, expected_revision: batch.revision },
            `${action}:${batch.batch_id}:${batch.revision}`,
          ),
        );
        row.append(control);
      };
      if ((batch.actions || []).includes("approve")) button("approve", "批准整批");
      if ((batch.actions || []).includes("reject")) button("reject", "拒绝整批");
      if ((batch.actions || []).includes("retry")) {
        row.append(
          node("p", "重试：候选仍有效时只重试提交；否则会重新调用主模型并可能产生费用。"),
        );
        button("retry", "重试");
      }
      if (batch.status === "awaiting_approval" || batch.status === "failed") {
        const show = node("button", "查看候选");
        show.addEventListener("click", () => void candidate(batch.batch_id));
        row.append(show);
      }
      const detail = candidates.get(batch.batch_id);
      if (typeof detail === "string") row.append(node("p", detail));
      else if (detail && !detail.candidate_available)
        row.append(node("p", "候选不可用：已完成、失效或依赖已删，不显示旧正文。"));
      else if (detail) {
        const plan = node("div");
        plan.setAttribute("aria-label", "候选差异");
        for (const item of detail.plan?.semantic || [])
          plan.append(
            node("p", `${item.action}${item.id ? " " + item.id : ""}${item.subject ? " · " + item.subject : ""}${item.fact ? "：" + item.fact : ""}`),
          );
        plan.append(node("p", `情景摘要：${detail.episode_summary}`));
        row.append(plan);
      }
      output.append(row);
    }
  }
  /** @param {string} id */
  function openBatch(id) {
    void candidate(id);
    section.scrollIntoView({ block: "start" });
  }
  return { section, load, openBatch, render };
}

/** @param {{sync:MemorySync, csrf:()=>string}} page */
function mirrorsPanel(page) {
  const section = node("section");
  section.setAttribute("aria-label", "Markdown 镜像");
  const note = node("div");
  note.setAttribute("role", "status");
  const readError = node("p");
  const output = node("div");
  section.append(
    node("h2", "Markdown 镜像"),
    node(
      "p",
      "单向只读派生，不导入修改。自动刷新、重建和清理期间请停止外部编辑；指纹检查不是任意并发写入的保证。",
    ),
    note,
    readError,
    output,
  );
  /** @type {Map<string,string>} */ const previews = new Map();
  /** @type {Wire[]} */ let mirrors = [];
  let request = 0;
  async function load() {
    const token = ++request;
    for (const name of previews.keys()) previews.set(name, "正在重新核验预览…");
    render();
    const result = await page.sync.read("/api/memory/mirrors", {});
    if (token !== request || result.state === "stale") return;
    mirrors = result.state === "ok" ? result.body.mirrors : [];
    readError.textContent =
      result.state === "failed" ? `镜像状态暂不可读取（${result.code}）。` : "";
    render();
    for (const name of previews.keys()) void preview(name);
  }
  /** @param {string} name */
  async function preview(name) {
    previews.set(name, "正在读取预览…");
    render();
    const result = await page.sync.read("/api/memory/mirrors", { name, preview: "1" });
    if (result.state === "stale" || !previews.has(name)) return;
    const mirror = result.state === "ok" ? result.body.mirrors[0] : null;
    previews.set(
      name,
      mirror?.ready && typeof mirror.text === "string"
        ? mirror.text
        : "当前镜像未核验或不可读，不显示旧内容。",
    );
    render();
  }
  const act = actionRunner({
    note,
    readPath: "/api/memory/mirrors",
    csrf: page.csrf,
    describe: (result) => `镜像操作完成：${result?.status}。`,
    settled: () => void load(),
  });
  function render() {
    output.replaceChildren();
    if (!page.sync.online) {
      output.append(node("p", "连接中断：镜像预览已隐藏，重连并核验后再显示。"));
      return;
    }
    for (const mirror of mirrors) {
      const name = mirror.name;
      const row = node("article");
      row.className = "card";
      row.dataset.mirror = name;
      row.append(
        node("strong", name === "facts" ? "事实镜像" : "情景镜像"),
        node("p", `路径 ${mirror.path ?? "未知"}`),
        node(
          "p",
          mirror.conflict
            ? "检测到外部修改：已停止覆盖，等待确认重新生成。"
            : mirror.error
              ? `同步失败（${mirror.error}）：数据库中的记忆不受影响。`
              : mirror.ready
                ? "已同步并核验。"
                : "待同步或核验中。",
        ),
        node("small", `生成 ${mirror.generated_at ?? "未知"} · 核验 ${mirror.verified_at ?? "未知"}`),
      );
      const show = node("button", "预览");
      show.addEventListener("click", () => void preview(name));
      row.append(show);
      if (!mirror.conflict && (mirror.error || mirror.dirty)) {
        const retry = node("button", "同步重试");
        retry.addEventListener("click", () =>
          void act({ action: "mirror_retry", name }, `retry:${name}`),
        );
        row.append(retry);
      }
      if (mirror.conflict && mirror.confirmation_token) {
        const confirm = node("button", "确认重新生成");
        // Returned verbatim: its 64-bit file identity would be rounded if
        // parsed into JavaScript numbers.
        confirm.addEventListener("click", () =>
          void act(
            { action: "mirror_confirm", name, observation: mirror.confirmation_token },
            `confirm:${name}:${mirror.confirmation_token}`,
          ),
        );
        row.append(confirm);
      }
      const text = previews.get(name);
      if (text !== undefined) row.append(node("pre", text));
      output.append(row);
    }
  }
  return { section, load, render };
}

/**
 * The Memory page: three tabs plus the statistics, queue, mirror and receipt
 * panels. MainBar and Session stay in the shell.
 * @param {HTMLElement} root @param {MemorySync} sync
 * @param {{csrf:()=>string, session:()=>string|null, receipts:ReturnType<typeof memoryReceipts>}} options
 */
export function memoryPage(root, sync, options) {
  const connection = node("p");
  connection.setAttribute("role", "status");
  const verify = node("button", "重新核验");
  verify.addEventListener("click", () => void sync.connected(sync.instance));
  const tabs = node("div");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "记忆类别");
  const receipts = options.receipts;
  receipts.attach({
    changed: () => refresh(),
    confirmed: (entry) => {
      semantic.confirmed(entry);
      episodic.confirmed(entry);
    },
  });
  const queue = queuePanel({ sync, csrf: options.csrf });
  const mirrors = mirrorsPanel({ sync, csrf: options.csrf });
  const statistics = statisticsPanel(options.session);
  const pageContext = {
    sync,
    submit: receipts.submit,
    openBatch: queue.openBatch,
    discard: receipts.discard,
  };
  const semantic = recordPanel("semantic", pageContext);
  const episodic = recordPanel("episodic", pageContext);
  const skillsRoot = node("div");
  const skills = skillsPanel(skillsRoot);
  /** @type {Array<[string,string,HTMLElement]>} */
  const panels = [
    ["semantic", "语义记忆", semantic.panel],
    ["episodic", "情景记忆", episodic.panel],
    ["skills", "Skill 目录", skillsRoot],
  ];
  const sections = panels.map(([key, label, content]) => {
    const tab = node("button", label);
    tab.type = "button";
    tab.id = `memory-tab-${key}`;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", `memory-panel-${key}`);
    const panel = node("section");
    panel.id = `memory-panel-${key}`;
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tab.id);
    panel.append(content);
    tab.addEventListener("click", () => select(key));
    tabs.append(tab);
    return { key, tab, panel };
  });
  /** @param {string} key */
  function select(key) {
    for (const item of sections) {
      const active = item.key === key;
      item.tab.setAttribute("aria-selected", String(active));
      item.tab.tabIndex = active ? 0 : -1;
      item.panel.hidden = !active;
    }
    const url = new URL(location.href);
    url.searchParams.set("tab", key);
    history.replaceState(null, "", url);
    if (key === "skills") void skills.load();
  }
  tabs.addEventListener("keydown", (event) => {
    const index = sections.findIndex((item) => item.tab === document.activeElement);
    if (index < 0 || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const next = sections[(index + (event.key === "ArrowRight" ? 1 : sections.length - 1)) % sections.length];
    select(next.key);
    next.tab.focus();
  });
  root.append(
    connection,
    verify,
    tabs,
    ...sections.map((item) => item.panel),
    receipts.section,
    statistics.section,
    queue.section,
    mirrors.section,
  );
  const requested = new URLSearchParams(location.search).get("tab") || "semantic";
  select(sections.some((item) => item.key === requested) ? requested : "semantic");
  function status() {
    connection.textContent = sync.online
      ? ""
      : sync.state === "unverified"
        ? "记忆状态核验失败：正文已隐藏，可重新核验。"
        : sync.state === "verifying" || !sync.instance
          ? "正在核验记忆状态…"
          : "连接中断：记忆正文、候选与镜像预览已隐藏，重连并核验后再显示。";
    verify.hidden = sync.state !== "unverified";
  }
  function refresh() {
    status();
    semantic.refresh();
    episodic.refresh();
    if (!sync.online) {
      queue.render();
      mirrors.render();
      return;
    }
    void queue.load();
    void mirrors.load();
    void receipts.refresh();
  }
  // The shell's #page element is reused by every page; this instance is gone
  // once its own tab list has been replaced.
  const unwatch = sync.watch(() => {
    if (!tabs.isConnected) {
      unwatch();
      return;
    }
    refresh();
  });
  refresh();
  void statistics.load();
}
