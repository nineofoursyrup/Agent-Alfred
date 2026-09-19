import {topologyView} from "./topology.js";
import {traceExport} from "./trace_export.js";
import {aggregationFacts} from "./aggregation.js";
import { node, textBlocks } from "./dom.js";
import { Pager } from "./pages.js";
import { interval, originLabel, sourceGroups } from "./memory.js";
/** @typedef {Record<string,any>} Wire */
/** @typedef {{expanded: Map<string, Wire>, toggle: (ref: Wire) => void}} References */
/** @param {Wire} run */
export function outcomeLabel(run) {
  if (run.reply_disposition === "no_reply") return "已结束 · 按要求未回复";
  const labels = /** @type {Record<string,string>} */ ({
    completed: "回复完成",
    max_steps: "受控停止",
    failed: "受控失败",
    interrupted: run.started_at === null ? "执行前中断" : "运行终态无法确认",
  });
  return (
    labels[run.outcome] || (run.phase === "accepted" ? "已接受" : "运行中")
  );
}

/** @param {HTMLElement} root @param {import('./progress.js').Progress} progress @param {()=>void} navigate @param {import('./memory.js').MemorySync} memory @param {()=>string} csrf @param {()=>Wire} runtime */
export function runsPage(root, progress, navigate, memory, csrf, runtime) {
  const context = new URLSearchParams(location.search);
  const filter = node("select");
  filter.setAttribute("aria-label", "运行筛选");
  for (const [value, label] of [
    ["chat", "对话"],
    ["system", "系统运行"],
    ["all", "全部"],
  ]) {
    const option = node("option", label);
    option.value = value;
    filter.append(option);
  }
  const requested = new URLSearchParams(location.search).get("filter") || "all";
  filter.value = ["chat", "system", "all"].includes(requested)
    ? requested
    : "all";
  const selected = location.pathname.startsWith("/runs/")
    ? decodeURIComponent(location.pathname.slice(6))
    : null;
  const list = node("section");
  list.setAttribute("aria-label", "运行列表");
  const pinned = node("div");
  const finished = node("div");
  list.append(pinned, finished);
  const detail = node("section");
  detail.setAttribute("aria-label", "运行过程");
  root.append(filter, list, detail);
  filter.addEventListener("change", () => {
    history.pushState(null, "", `/runs?filter=${filter.value}`);
    navigate();
  });
  let exportView = /** @type {ReturnType<typeof traceExport>|null} */ (null);
  const pathView = selected ? topologyView(root, "run-path", runtime, memory, selected) : null;
  const seen = new Set();
  /** @type {Wire|null} */ let selectedRun = null;
  /** @type {Wire|null} */ let evidence = null;
  /** @type {Wire|null} */ let live = null;
  /** @type {Wire|null} */ let globalActive = null;
  // Expanded references always re-read the current record; nothing from the
  // Run's time is shown as current, and invalidation hides what was shown.
  /** @type {Map<string, Wire>} */ const expanded = new Map();
  /** @param {string} key */
  async function current(key) {
    const entry = expanded.get(key);
    if (!entry) return;
    entry.state = memory.online ? "loading" : "offline";
    entry.record = null;
    update();
    if (!memory.online) return;
    const result = await memory.read("/api/memory/record", {
      kind: entry.ref.kind,
      id: entry.ref.memory_id,
    });
    if (expanded.get(key) !== entry || result.state === "stale") return;
    if (result.state === "ok") {
      entry.state = "current";
      entry.record = result.body.record;
    } else if (result.state === "failed") {
      entry.state = result.status === 404 ? "gone" : "failed";
      entry.code = result.code;
    } else entry.state = "offline";
    update();
  }
  /** @type {References} */ const references = {
    expanded,
    toggle(ref) {
      const key = referenceKey(ref);
      if (expanded.delete(key)) update();
      else {
        expanded.set(key, { ref });
        void current(key);
      }
    },
  };
  // #page outlives this page instance; its own detail section does not.
  const unwatch = memory.watch(() => {
    if (!detail.isConnected) {
      unwatch();
      return;
    }
    for (const key of expanded.keys()) void current(key);
  });
  /** @param {Wire|null} active */
  function sync(active) {
    globalActive = active;
    const previous = live?.run_id;
    const category = active?.purpose === "chat" ? "chat" : "system";
    if (filter.value !== "all" && filter.value !== category) active = null;
    live = active;
    pinned.replaceChildren();
    if (active) {
      const card = node("article");
      card.className = "card";
      card.append(
        node("strong", active.purpose),
        node("p", outcomeLabel(active)),
      );
      if (active.recording_state)
        card.append(
          node(
            "p",
            active.recording_state === "pending"
              ? "正在保存"
              : active.recording_state === "failed"
                ? "未保存"
                : "已保存",
          ),
        );
      const link = node("a", "查看运行");
      link.href = `/runs/${encodeURIComponent(active.run_id)}?filter=${active.purpose === "chat" ? "chat" : "system"}`;
      card.append(link);
      pinned.append(card);
      if (active.run_id === selected) {
        selectedRun = { ...selectedRun, ...active };
        card.dataset.highlighted = "true";
      }
    }
    if (previous && previous !== active?.run_id) void refresh();
    update();
  }
  async function refresh() {
    try {
      const response = await fetch(
        "/api/runs?" +
          new URLSearchParams({ filter: filter.value, limit: "25" }),
      );
      if (response.ok && root.isConnected) append(await response.json());
    } catch {
      /* Existing page remains readable; its more button can retry. */
    }
  }
  /** @param {Wire} body */
  function append(body) {
    if (!root.isConnected) return;
    if (filter.value !== body.filter) {
      filter.value = body.filter;
      sync(globalActive);
    }
    if (body.non_terminal && !live) sync(body.non_terminal);
    for (const run of body.runs) {
      if (seen.has(run.run_id)) continue;
      seen.add(run.run_id);
      const card = node("article");
      card.className = "card";
      // The API's purpose is already escaped display text. Decode entities in
      // an inert textarea, then put only textContent in the document.
      const decoded = node("textarea");
      decoded.innerHTML = run.purpose;
      card.append(
        node("strong", decoded.value),
        node("p", outcomeLabel(run)),
        node("small", `${run.gateway} · ${run.accepted_at}`),
      );
      if (run.admission_state === "unconfirmed")
        card.append(node("p", "准入未确认"));
      const open = node("a", "查看运行");
      open.href = `/runs/${encodeURIComponent(run.run_id)}?filter=${run.filter}`;
      card.append(open);
      finished.append(card);
      if (run.run_id === selected) {
        selectedRun = run;
        card.dataset.highlighted = "true";
        void loadEvidence();
      }
    }
    if (body.non_terminal?.run_id === selected) {
      selectedRun = body.non_terminal;
      void loadEvidence();
    }
  }
  const pager = new Pager(
    root,
    "/api/runs",
    { filter: filter.value, limit: "25" },
    append,
    "更多运行",
  );
  async function loadEvidence() {
    if (selected === null) return;
    try {
      const response = await fetch(
        "/api/run-evidence?" + new URLSearchParams({ run_id: selected, ...(context.get("snapshot_id") ? {snapshot_id: context.get("snapshot_id") || ""} : {}) }),
      );
      const body = await response.json();
      if (!root.isConnected) return;
      if (response.status === 410 && body.error?.code === "snapshot_expired") {
        evidence = { snapshot_expired: true };
        update();
        return;
      }
      if (!response.ok || body.run_id !== selected)
        throw new Error("过程不可用");
      evidence = body;
    } catch {
      evidence = { trace_status: "unavailable", events: [], attempts: [] };
    }
    update();
  }
  function update() {
    if (!selectedRun || !root.isConnected) return;
    if (selected && !exportView) exportView=traceExport(root,selected,csrf,memory);
    const events = new Map();
    for (const event of evidence?.events || []) events.set(event.seq, event);
    for (const [seq, event] of progress.events.get(selectedRun.run_id) || [])
      events.set(seq, event);
    const prior = new Map([...detail.querySelectorAll("details")].map(item =>
      [item.dataset.attempt, {open: item.open, aborted: item.classList.contains("aborted")}]
    ));
    const focused = document.activeElement;
    const focusAttempt = focused?.tagName === "SUMMARY" && detail.contains(focused)
      ? /** @type {HTMLElement} */ (focused.parentElement).dataset.attempt : null;
    const focusReference = focused instanceof HTMLElement && detail.contains(focused)
      ? focused.dataset.reference : undefined;
    detail.replaceChildren(node("h2", "过程证据"), node("p", "事件发布顺序"));
    if (evidence?.snapshot_expired) {
      const filters = new URLSearchParams({snapshot_context: context.has("ops_range") ? "expired" : "missing"});
      for (const key of ["range", "timezone", "start", "end", "session_id", "purpose", "tool", "run_id"])
        if (context.has("ops_" + key)) filters.set(key, context.get("ops_" + key) || "");
      const refresh = node("a", "返回账本核对筛选并刷新");
      refresh.href = "/ops?" + filters;
      detail.append(node("p", "账目快照已失效；尚未核验当前过程记录。"), refresh);
      return;
    }
    if (evidence?.trace_incomplete === true)
      detail.append(node("p", "追踪不完整"));
    if (evidence?.recording_state === "recorded")
      detail.append(node("p", "已保存"));
    if (evidence && evidence.trace_status !== "available")
      detail.append(
        node(
          "p",
          evidence.trace_status === "live"
            ? "实时过程由 SSE 提供"
            : evidence.trace_status === "pruned"
            ? "过程记录已裁剪"
            : evidence.trace_status === "partial"
              ? "过程记录不完整"
              : evidence.trace_status === "too_large"
                ? "过程记录超过读取上限，未加载"
                : "过程记录不可用",
        ),
      );
    if (evidence?.memory?.aggregation) aggregationFacts(detail, evidence.memory.aggregation, memory);
    if (evidence?.memory?.routing) {
      const routing = evidence.memory.routing;
      const section = node('section');
      section.append(node('h3', '消息分流'),
        node('p', `${routing.route || '未执行图'} · ${routing.decision_reason || routing.fallback?.reason || ''}`),
        node('p', `图结果：${routing.graph_result || '未执行'}；代际 ${routing.generation ?? '无'}`));
      if (routing.reply_disposition === 'no_reply') section.append(node('p', '已结束 · 按要求未回复'));
      if (routing.recoveries?.length) {
        section.append(node('p', '上下文准备失败，已降级处理。'));
        for (const recovery of routing.recoveries)
          section.append(node('p', `${recovery.node_id} · ${recovery.code} · ${recovery.message} · ${recovery.side_effect_state}`));
      }
      if (routing.classification?.actual_model) section.append(node('p', `分类模型：${routing.classification.actual_model.endpoint_id} / ${routing.classification.actual_model.model_id}`));
      if (routing.fallback?.decision !== 'not_needed') section.append(node('p', `普通回退：${routing.fallback?.decision} / ${routing.fallback?.reason}`));
      detail.append(section);
    }
    renderInputs(detail, evidence?.memory, references, evidence?.trace_incomplete ? "partial" : evidence?.trace_status);
    const selectedId = selectedRun.run_id;
    const confirmed = new Set(
      [...progress.attempts.values()]
        .filter(item => item.run_id === selectedId && item.dispatched)
        .map(item => item.attempt_id),
    );
    const runFinished = selectedRun.phase === "finished" || [...events.values()].some(
      event => event.payload.name === "run.finished");
    const authoritative = evidence?.memory?.input_preparation &&
      evidence.trace_status !== "live" && runFinished;
    const actual = new Set([
      ...(evidence?.attempts || []), ...(evidence?.memory?.input_attempts || []),
    ].map((/** @type {Wire} */ item) => item.attempt_id));
    for (const event of events.values()) {
      if (["attempt.committed", "attempt.aborted"].includes(event.payload.name))
        actual.add(event.envelope.attempt_id);
    }
    for (const id of confirmed) actual.add(id);
    for (const id of actual) confirmed.add(id);
    renderEvidence(
      detail,
      [...events.values()].filter(event => !authoritative ||
        !event.envelope.attempt_id || actual.has(event.envelope.attempt_id)),
      evidence?.attempts || [],
      runFinished ? {...selectedRun, phase: "finished"} : selectedRun,
      confirmed,
    );
    for (const item of detail.querySelectorAll("details")) {
      const previous = prior.get(item.dataset.attempt);
      if (previous && !(item.classList.contains("aborted") && !previous.aborted))
        item.open = previous.open;
      if (focusAttempt && focusAttempt === item.dataset.attempt)
        item.querySelector("summary")?.focus({preventScroll: true});
    }
    if (focusReference !== undefined)
      for (const button of detail.querySelectorAll("button"))
        if (button.dataset.reference === focusReference) {
          button.focus({preventScroll: true});
          break;
        }
  }
  if (selected !== null) {
    void (async () => {
      try {
        const response = await fetch(
          `/api/runs/locate/${encodeURIComponent(selected)}?limit=25`,
        );
        const body = await response.json();
        if (!root.isConnected) return;
        if (!response.ok) {
          list.append(node("p", "运行不存在或暂不可读取"));
          pager.button.hidden = true;
          return;
        }
        pager.params.filter = body.filter;
        pager.cursor = body.next_cursor || "";
        pager.finished = !body.next_cursor;
        pager.button.hidden = pager.finished;
        append(body);
      } catch {
        if (root.isConnected) list.append(node("p", "运行暂不可读取"));
      }
    })();
  } else void pager.load();
  return { update, loadEvidence, sync, dispose: () => {exportView?.dispose();pathView?.close();} };
}

/** @param {HTMLElement} root @param {Wire[]} events @param {Wire[]} ledger @param {Wire} run @param {Set<string>} confirmed */
function renderEvidence(root, events, ledger, run, confirmed) {
  const shownAccounts = new Set();
  /** @type {Map<number,Wire[]>} */ const steps = new Map();
  for (const event of events.sort((a, b) => a.seq - b.seq)) {
    const index = event.envelope.step_index;
    if (index === null || index === undefined) continue;
    if (!steps.has(index)) steps.set(index, []);
    steps.get(index)?.push(event);
  }
  for (const [index, facts] of [...steps].sort((a, b) => a[0] - b[0])) {
    const section = node("section");
    section.className = "card";
    section.id = `step-${index}`;
    section.append(node("h3", `Step ${index}`));
    const preparation = facts.find(fact => fact.payload.name === "step.started");
    const skillSystem = preparation?.payload.skill_system ||
      (preparation?.payload.system || []).filter((/** @type {Wire} */ block) =>
        block.type === "text" && block.text?.startsWith("<skills>\n")).map((/** @type {Wire} */ block) => block.text);
    if (skillSystem.length) {
      const prepared = node("details");
      prepared.dataset.attempt = `skill-system-${index}`;
      prepared.append(node("summary", "准备时的 Skill 区段（脱敏追踪，不单独证明发送）"));
      for (const text of skillSystem) prepared.append(node("pre", text));
      section.append(prepared);
    }
    const committed = facts.find(
      (fact) => fact.payload.name === "attempt.committed",
    );
    section.append(
      node(
        "p",
        committed
          ? "主结果 · committed"
          : run.phase === "finished"
            ? "现有过程证据未见已提交尝试"
            : "等待尝试收尾",
      ),
    );
    if (committed)
      section.append(node("p", textBlocks(committed.payload.blocks)));
    const anchors = facts.filter(fact => fact.payload.name === "attempt.started");
    for (const fact of facts) {
      if (["attempt.committed", "attempt.aborted"].includes(fact.payload.name) &&
          !anchors.some(anchor => anchor.envelope.attempt_id === fact.envelope.attempt_id))
        anchors.push(fact);
    }
    for (const anchor of anchors.sort((a, b) => a.seq - b.seq)) {
      const id = anchor.envelope.attempt_id;
      const transitions = facts.filter(
        (fact) => fact.envelope.attempt_id === id,
      );
      const start = transitions.find(fact => fact.payload.name === "attempt.started");
      const terminal = transitions.find((fact) =>
        ["attempt.committed", "attempt.aborted"].includes(fact.payload.name),
      );
      const accounting = ledger.find((attempt) => attempt.attempt_id === id);
      const actual = Boolean(terminal || accounting || confirmed.has(id));
      const details = node("details");
      details.dataset.attempt = id;
      details.id = `attempt-${id}`;
      const aborted = terminal?.payload.name === "attempt.aborted";
      details.className = !actual ? "input-preparation" : aborted ? "attempt aborted" : "attempt";
      details.open = !aborted;
      details.append(
        node(
          "summary",
          actual ? `Attempt · seq ${anchor.seq} · ${aborted ? "aborted（已撤回）" : terminal ? "committed" : run.phase === "finished" ? "结果未知" : "运行中"}`
            : `输入准备 · seq ${anchor.seq} · 发送待确认`,
        ),
      );
      if (aborted) details.append(node("p", "未进入回答但已产生 Token／费用"));
      if (start?.payload.model)
        details.append(
          node(
            "p",
            `${start.payload.model.endpoint_id} / ${start.payload.model.model_id}`,
          ),
        );
      const streamed = start?.payload.streamed;
      details.append(node("p", streamed === true ? "流式" : streamed === false ? "非流式" : "流式状态未知"));
      for (const transition of transitions)
        details.append(
          node("p", `seq ${transition.seq} · ${transition.payload.name}`),
        );
      if (terminal) {
        const payload = terminal.payload;
        details.append(
          node(
            "p",
            `${payload.duration_ms ?? "未知"} ms · ${payload.stop_reason || payload.error?.code || ""}`,
          ),
        );
        details.append(node("p", textBlocks(payload.blocks)));
        const counts = new Map();
        for (const block of payload.blocks || [])
          if (block.type !== "text")
            counts.set(block.type, (counts.get(block.type) || 0) + 1);
        for (const [type, count] of counts)
          details.append(node("p", `${type} × ${count}`));
      }
      if (actual) renderAccounting(details, accounting);
      if (accounting) shownAccounts.add(id);
      section.append(details);
    }
    root.append(section);
  }
  const unplaced = ledger.filter(attempt => !shownAccounts.has(attempt.attempt_id));
  if (unplaced.length) {
    const accounting = node("section");
    accounting.append(node("h3", "调用账目 · 过程顺序不可用"));
    for (const attempt of unplaced) {
      const row = node("article");
      row.append(node("h4", `Attempt ${attempt.attempt_id} · ${attempt.outcome}`));
      renderAccounting(row, attempt);
      accounting.append(row);
    }
    root.append(accounting);
  }
}
/** @param {HTMLElement} root @param {Wire|undefined} accounting */
function renderAccounting(root, accounting) {
  const usage = accounting?.usage || {};
  for (const key of ["total_input_tokens", "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens"])
    root.append(node("small", `${key}: ${usage[key] ?? "未提供"} `));
  renderCost(root, accounting?.cost || {state: "unknown"});
}
/** @param {HTMLElement} root @param {Wire} charge */
function renderCost(root, charge) {
  if (charge.state === "unknown") {
    root.append(node("p", "unknown · 费用未知"));
    return;
  }
  root.append(node("p", `${charge.state} · USD ${charge.amount}`));
  if (charge.state === "estimated") {
    const components = charge.price_components || [];
    let tiered = false;
    for (const component of components) {
      root.append(
        node(
          "small",
          `${component.dimension}: ${component.price_source || component.source}${component.stale ? " · stale" : ""} `,
        ),
      );
      if (component.tiered) tiered = true;
    }
    if (tiered) root.append(node("p", "按基础档估算"));
  }
}

/** @param {Wire} ref */
function referenceKey(ref) {
  return [ref.kind, ref.memory_id, ref.record_version].join("\u0000");
}

/** @param {Wire} ref @param {string} label @param {References} references */
function referenceRow(ref, label, references) {
  const row = node("div");
  const key = referenceKey(ref);
  const entry = references.expanded.get(key);
  row.append(node("p", label));
  const toggle = node("button", entry ? "收起当前内容" : "查看当前内容");
  toggle.dataset.reference = `${label}\u0000${key}`;
  toggle.addEventListener("click", () => references.toggle(ref));
  row.append(toggle);
  if (!entry) return row;
  const record = entry.record;
  row.append(
    entry.state === "offline"
      ? node("p", "连接中断：正文已隐藏，重连并核验后再显示。")
      : entry.state === "gone"
        ? node("p", "记录已不存在（可能已被删除）；不显示旧正文。")
        : entry.state === "failed"
          ? node("p", `读取失败（${entry.code}）：不能确认记录是否存在。`)
          : entry.state !== "current" || !record
            ? node("p", "正在读取当前内容…")
            : node(
                "p",
                record.record_version === ref.record_version
                  ? "当前内容与引用版本一致"
                  : `当前展示为修改后内容（当前版本 ${record.record_version}，引用时版本 ${ref.record_version}）`,
              ),
  );
  if (entry.state === "current" && record)
    row.append(
      node(
        "p",
        record.kind === "semantic"
          ? `${record.subject}：${record.fact}`
          : `${record.summary}（${interval(record)}）`,
      ),
      node(
        "small",
        `创建：${originLabel(record.origin)} · 最近修改：${originLabel(record.last_change_origin)} · ${record.modified_at}`,
      ),
      sourceGroups(record.provenance),
    );
  return row;
}

/** @param {HTMLElement} root @param {Wire} gate @param {References} references */
function renderGate(root, gate, references) {
  const section = node("section");
  section.append(node("h3", "检索门"));
  section.append(
    node(
      "p",
      `结果 ${gate.outcome} · ${gate.decision_source === "model" ? "模型判断" : "确定性规则"} · 原因 ${gate.reason_code}${gate.fallback_reason ? " · 回退原因 " + gate.fallback_reason : ""}${gate.rule_version ? " · 规则 " + gate.rule_version : ""}`,
    ),
  );
  for (const [name, label] of [["semantic", "语义库"], ["episodic", "情景库"]]) {
    const store = gate.stores?.[name];
    if (store)
      section.append(
        node(
          "p",
          `${label} ${store.status}${store.hit_count === null ? "" : " · 召回 " + store.hit_count}${store.error_code ? " · " + store.error_code : ""}`,
        ),
      );
  }
  section.append(
    node(
      "p",
      `召回 ${gate.hit_count ?? "未知"} 条 · 选入参考资料 ${gate.selected_count} 条 · 输入处置 ${gate.input_disposition}。选入不等于已发送，实际携带见下方各请求。`,
    ),
  );
  for (const ref of gate.references || [])
    section.append(
      referenceRow(
        ref,
        `${ref.kind === "semantic" ? "语义" : "情景"} 第 ${ref.rank} 条 · ${ref.memory_id} · 引用版本 ${ref.record_version} · ${originLabel(ref.origin)} · ${ref.selected ? "已选入" : `未选入（${ref.omission_reason}）`}`,
        references,
      ),
    );
  root.append(section);
}

/** @param {HTMLElement} root @param {Wire|undefined} memory @param {References} references @param {string|undefined} traceStatus */
function renderInputs(root, memory, references, traceStatus) {
  const details = node("details");
  details.dataset.attempt = "input-explanation";
  details.append(node("summary", "本次输入"));
  details.append(node("p", "字符限额是本地输入检查，不保证供应商 Token 容量。"));
  if (!memory || memory.gate_state === "legacy_unknown") {
    details.append(node("p", "输入说明未知或暂不可读取"));
  }
  if (memory?.gate_state === "evaluated" && memory.gate)
    renderGate(details, memory.gate, references);
  else if (memory?.gate_state === "not_evaluated")
    details.append(node("p", "本次未评估检索门"));
  else if (memory?.gate_state === "incomplete")
    details.append(node("p", "检索门评估未完成，不计入统计"));
  if (memory?.input_evidence_error) {
    details.append(node("p", "输入来源或读取登记暂不可确认；该请求未发送。请检查存储状态后重试。"));
  }
  for (const pending of memory?.input_unconfirmed || []) {
    details.append(node("p", `输入登记待恢复 · ${pending.purpose} · Step ${pending.step_index} · 请求 ${pending.attempt_id}；来源登记未确认，不计作已确认输入。`));
  }
  const skills = memory?.skills;
  if (skills) {
    details.append(node("h3", "Skill"));
    details.append(node("p", `选择方式：${skills.mode} · ${skills.status === "failed" ? "准备失败" : skills.status === "prepared" ? "准备完成，未单独证明发送" : "准备未完成"}`));
    details.append(node("p", `所选顺序：${skills.selected?.join(" → ") || "无"}`));
    if (skills.reason) details.append(node("p", `选择说明：${skills.reason}`));
    for (const skill of skills.loaded || [])
      details.append(node("p", `${skill.name} · ${skill.source} · ${skill.overrides_builtin ? "整体覆盖内置" : "未覆盖"} · ${skill.codepoints} 码点 · ${skill.fingerprint_version} ${skill.body_sha256}`));
    for (const excluded of skills.excluded || [])
      details.append(node("p", `未加载 ${excluded.name}：${excluded.reason}`));
    if (skills.loaded?.length) details.append(node("p", traceStatus === "available" ? "当时正文请展开对应 Step 的脱敏追踪；当前目录不回填历史版本。" : "当时 Skill 正文追踪不可用或不完整；当前目录不能证明历史正文。"));
  } else details.append(node("p", "Skill 选择证据未知或不可用"));
  const selector = memory?.skill_input_preparation;
  if (selector) details.append(node("p", `选择器输入 ${selector.status} · ${selector.characters} / ${selector.limit} 字符 · 历史预算排除 ${selector.budget_omitted_groups} 组`));
  const preparation = memory?.input_preparation;
  if (preparation && preparation.purpose !== "skill_selector") {
    details.append(node("p", `${preparation.status === "failed" ? "输入准备失败" : "容量选择"} · ${preparation.measurement_version}`));
    details.append(node("p", `回答已有 ${preparation.answer_characters} 字符，预留 ${preparation.reserved_characters} 字符，上限 ${preparation.answer_limit}；检索门 ${preparation.gate_characters} / ${preparation.gate_limit}。预留不计作实际发送。`));
    const excluded = preparation.history_exclusions;
    details.append(node("p", `准备阶段历史排除：不完整 ${excluded?.incomplete ?? "未知"}，隔离或来源未确认 ${excluded?.unsafe ?? "未知"}，N 上限 ${excluded?.round_limit ?? "未知"}，字符预算 ${preparation.budget_omitted_groups ?? "未知"}。`));
    details.append(node("p", `准备阶段工具账因限额省略 ${preparation.ledger_omitted ?? "未知"} 条，其中结果未知 ${preparation.ledger_unknown_omitted ?? "未知"} 条；隔离或失效排除 ${preparation.ledger_excluded ?? "未知"} 条。容量选择不计作实际发送，有限摘要不能证明动作未发生。`));
  }
  if (memory?.input_failure) {
    const failure = memory.input_failure;
    details.append(node("p", `输入准备失败 · ${failure.purpose === "skill_selector" ? "Skill 选择器" : `Step ${failure.step_index ?? "未知"}`} · ${failure.characters} 字符 / 上限 ${failure.limit}；未发送该请求。`));
    const excluded = failure.history_exclusions;
    details.append(node("p", `本次失败准备历史排除：不完整 ${excluded?.incomplete ?? "未知"}，隔离或来源未确认 ${excluded?.unsafe ?? "未知"}，N 上限 ${excluded?.round_limit ?? "未知"}，字符预算 ${failure.budget_omitted_groups ?? "未知"}。`));
    if (failure.purpose !== "skill_selector") details.append(node("p", `本次失败准备工具账因限额省略 ${failure.ledger_omitted ?? "未知"} 条，其中结果未知 ${failure.ledger_unknown_omitted ?? "未知"} 条；隔离或失效排除 ${failure.ledger_excluded ?? "未知"} 条。不计作实际发送。`));
  }
  for (const input of memory?.input_attempts || []) {
    const section = node("section");
    section.append(node("h3", `${input.purpose} · Step ${input.step_index} · Attempt ${input.attempt_id}`));
    section.append(node("p", `${input.measurement_version ?? "计量未知"} · ${input.input_characters ?? "未知"} 字符 / 上限 ${input.input_limit ?? "未知"}`));
    const omitted = input.history_exclusions;
    if (omitted) section.append(node("p", `历史排除：不完整 ${omitted.incomplete}，隔离或来源未确认 ${omitted.unsafe}，N 上限 ${omitted.round_limit}，字符预算 ${input.budget_omitted_groups}。`));
    for (const skill of input.skills || [])
      section.append(node("p", `实际请求携带 Skill ${skill.name} · ${skill.source} · ${skill.fingerprint_version} ${skill.body_sha256}`));
    for (const ref of input.references || [])
      section.append(
        referenceRow(
          ref,
          `实际请求携带 ${ref.kind === "semantic" ? "语义" : "情景"}记忆 ${ref.memory_id} · 版本 ${ref.record_version}`,
          references,
        ),
      );
    for (const id of input.working_history_groups || []) {
      const link = node("a", `历史 Run ${id}`);
      link.href = `/runs/${encodeURIComponent(id)}`;
      section.append(link);
    }
    for (const entry of input.ledger_entries || []) {
      section.append(node("p", `工具账 ${entry.ledger_id} · ${entry.action} · ${entry.status} · ${entry.at} · 来源 Run ${entry.source_run}`));
    }
    if (input.purpose === "answer") section.append(node("p", `工具账因限额省略 ${input.ledger_omitted ?? "未知"} 条，其中结果未知 ${input.ledger_unknown_omitted ?? "未知"} 条；隔离或失效排除 ${input.ledger_excluded ?? "未知"} 条。有限摘要不能证明动作未发生。`));
    details.append(section);
  }
  root.append(details);
}
