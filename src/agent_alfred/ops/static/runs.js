import { node, textBlocks } from "./dom.js";
import { Pager } from "./pages.js";
/** @typedef {Record<string,any>} Wire */
/** @param {Wire} run */
export function outcomeLabel(run) {
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

/** @param {HTMLElement} root @param {import('./progress.js').Progress} progress @param {()=>void} navigate */
export function runsPage(root, progress, navigate) {
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
  const seen = new Set();
  /** @type {Wire|null} */ let selectedRun = null;
  /** @type {Wire|null} */ let evidence = null;
  /** @type {Wire|null} */ let live = null;
  /** @type {Wire|null} */ let globalActive = null;
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
        "/api/run-evidence?" + new URLSearchParams({ run_id: selected }),
      );
      const body = await response.json();
      if (!root.isConnected) return;
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
    detail.replaceChildren(node("h2", "过程证据"), node("p", "事件发布顺序"));
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
    renderInputs(detail, evidence?.memory);
    renderEvidence(
      detail,
      [...events.values()],
      evidence?.attempts || [],
      selectedRun,
    );
    for (const item of detail.querySelectorAll("details")) {
      const previous = prior.get(item.dataset.attempt);
      if (previous && !(item.classList.contains("aborted") && !previous.aborted))
        item.open = previous.open;
      if (focusAttempt && focusAttempt === item.dataset.attempt)
        item.querySelector("summary")?.focus({preventScroll: true});
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
  return { update, loadEvidence, sync };
}

/** @param {HTMLElement} root @param {Wire[]} events @param {Wire[]} ledger @param {Wire} run */
function renderEvidence(root, events, ledger, run) {
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
    section.append(node("h3", `Step ${index}`));
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
    for (const start of facts.filter(
      (fact) => fact.payload.name === "attempt.started",
    )) {
      const id = start.envelope.attempt_id;
      const transitions = facts.filter(
        (fact) => fact.envelope.attempt_id === id,
      );
      const terminal = transitions.find((fact) =>
        ["attempt.committed", "attempt.aborted"].includes(fact.payload.name),
      );
      const accounting = ledger.find((attempt) => attempt.attempt_id === id);
      const details = node("details");
      details.dataset.attempt = id;
      const aborted = terminal?.payload.name === "attempt.aborted";
      details.className = aborted ? "attempt aborted" : "attempt";
      details.open = !aborted;
      details.append(
        node(
          "summary",
          `Attempt · seq ${start.seq} · ${aborted ? "aborted（已撤回）" : terminal ? "committed" : "运行中"}`,
        ),
      );
      if (aborted) details.append(node("p", "未进入回答但已产生 Token／费用"));
      if (start.payload.model)
        details.append(
          node(
            "p",
            `${start.payload.model.endpoint_id} / ${start.payload.model.model_id}`,
          ),
        );
      details.append(node("p", start.payload.streamed ? "流式" : "非流式"));
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
      renderAccounting(details, accounting);
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

/** @param {HTMLElement} root @param {Wire|undefined} memory */
function renderInputs(root, memory) {
  const details = node("details");
  details.dataset.attempt = "input-explanation";
  details.append(node("summary", "本次输入"));
  details.append(node("p", "字符限额是本地输入检查，不保证供应商 Token 容量。"));
  if (!memory || memory.gate_state === "legacy_unknown") {
    details.append(node("p", "输入说明未知或暂不可读取"));
  }
  if (memory?.input_evidence_error) {
    details.append(node("p", "输入来源或读取登记暂不可确认；该请求未发送。请检查存储状态后重试。"));
  }
  const preparation = memory?.input_preparation;
  if (preparation) {
    details.append(node("p", `${preparation.status === "failed" ? "输入准备失败" : "容量选择"} · ${preparation.measurement_version}`));
    details.append(node("p", `回答已有 ${preparation.answer_characters} 字符，预留 ${preparation.reserved_characters} 字符，上限 ${preparation.answer_limit}；检索门 ${preparation.gate_characters} / ${preparation.gate_limit}。预留不计作实际发送。`));
  }
  if (memory?.input_failure) {
    const failure = memory.input_failure;
    details.append(node("p", `输入准备失败 · Step ${failure.step_index} · ${failure.characters} 字符 / 上限 ${failure.limit}；未发送该请求。`));
  }
  for (const input of memory?.input_attempts || []) {
    const section = node("section");
    section.append(node("h3", `${input.purpose} · Step ${input.step_index} · Attempt ${input.attempt_id}`));
    section.append(node("p", `${input.measurement_version ?? "计量未知"} · ${input.input_characters ?? "未知"} 字符 / 上限 ${input.input_limit ?? "未知"}`));
    const omitted = input.history_exclusions;
    if (omitted) section.append(node("p", `历史排除：不完整 ${omitted.incomplete}，隔离或来源未确认 ${omitted.unsafe}，N 上限 ${omitted.round_limit}，字符预算 ${input.budget_omitted_groups}。`));
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
