import { topologyView } from "./topology.js";
import { traceExport } from "./trace_export.js";
import { aggregationFacts } from "./aggregation.js";
import { node } from "./dom.js";
import {
  sourceReader,
  follow,
  highlightSource,
  captureReading,
} from "./source.js";
import { runRow, runTableRow, RUN_COLUMNS } from "./run-fields.js";
import {
  renderEvidence,
  renderInputs,
  renderAccounting,
} from "./run-evidence.js";
export { outcomeLabel } from "./run-fields.js";
/** @typedef {Record<string,any>} Wire */

/** @param {HTMLElement} root @param {import('./progress.js').Progress} progress @param {Wire} dashboard @param {import('./memory.js').MemorySync} memory @param {()=>string} csrf */
export function runsPage(root, progress, dashboard, memory, csrf) {
  const runtime = dashboard.runtime,
    url = new URL(location.href),
    context = url.searchParams;
  const selected = url.pathname.startsWith("/runs/")
    ? decodeURIComponent(url.pathname.slice(6))
    : null;
  const filter = context.get("filter") ?? "all";
  let closed = false,
    restored = false,
    selectedRun = /** @type {Wire|null} */ (null),
    evidence = /** @type {Wire|null} */ (null);
  let origin = /** @type {Wire|null} */ (null),
    anchor = /** @type {string|null} */ (null),
    live = /** @type {Wire|null} */ (null);
  let sequence = 0,
    controller = new AbortController(),
    metadataBusy = false,
    evidenceBusy = false,
    evidenceError = "",
    metadataAt = 0,
    evidenceAt = 0;
  let metadataInvalidated = "",
    evidenceInvalidated = "";
  let evidenceVersion = 0,
    lastRender = "";
  const connection = runtime().instance;
  const notice = node("p");
  notice.setAttribute("role", "status");
  root.append(notice);
  const summary = node("section");
  summary.setAttribute("aria-label", "运行摘要");
  const detail = node("section");
  detail.setAttribute("aria-label", "运行过程");
  const list = node("section");
  list.setAttribute("aria-label", "运行列表");
  list.className = "run-source";
  const pinned = node("tbody"),
    finished = node("tbody"),
    controls = node("div");
  const table = node("table");
  table.className = "run-table";
  table.setAttribute("aria-label", "运行记录");
  const head = node("thead"),
    header = node("tr");
  for (const label of RUN_COLUMNS) {
    const cell = node("th", label);
    cell.scope = "col";
    header.append(cell);
  }
  head.append(header);
  table.append(head, pinned, finished);
  list.append(table, controls);
  const seen = new Set();
  const back = node("a", "返回运行列表");
  back.href = "/runs?" + new URLSearchParams({ filter });
  const locationNotice = node("p");
  locationNotice.setAttribute("role", "status");
  const mainbar = node("button", "在主对话中查看");
  mainbar.disabled = true;
  const refreshEvidence = node("button", "刷新过程证据");
  refreshEvidence.addEventListener("click", () => void loadEvidence());
  const refreshRun = node("button", "刷新运行摘要");
  refreshRun.addEventListener("click", () => {
    if (runtime().instance !== connection) location.reload();
    else void loadSelected();
  });
  const metadataStatus = node("p"),
    evidenceStatus = node("p");
  metadataStatus.setAttribute("role", "status");
  evidenceStatus.setAttribute("role", "status");
  let exportView = /** @type {ReturnType<typeof traceExport>|null} */ (null),
    pathView = /** @type {ReturnType<typeof topologyView>|null} */ (null);
  let closeAggregation = () => {};
  const usage = node("details"),
    usageBody = node("div"),
    exportMount = node("div");
  usage.append(node("summary", "用量"), usageBody);
  usage.id = "run-usage";
  exportMount.id = "trace-export";
  if (selected === null) {
    const select = node("select");
    select.setAttribute("aria-label", "运行筛选");
    for (const [value, label] of [
      ["all", "全部"],
      ["chat", "会话运行"],
      ["system", "系统运行"],
    ]) {
      const option = node("option", label);
      option.value = value;
      select.append(option);
    }
    select.value = filter;
    select.addEventListener(
      "change",
      () =>
        void dashboard.navigate(
          "/runs?" + new URLSearchParams({ filter: select.value }),
        ),
    );
    root.append(select, list);
  } else {
    const actions = node("div");
    actions.className = "run-actions";
    const exportLink = node("a", "查看追踪导出");
    exportLink.href = "#trace-export";
    follow(exportLink, () => exportView?.reveal());
    actions.append(back, mainbar, exportLink);
    root.append(
      actions,
      summary,
      metadataStatus,
      refreshRun,
      locationNotice,
      evidenceStatus,
      refreshEvidence,
      detail,
    );
    pathView = topologyView(root, "run-path", runtime, memory, selected);
    root.append(usage, exportMount);
    follow(
      back,
      () =>
        void dashboard.navigate(origin?.route || back.href, {
          source: origin || {},
        }),
    );
    mainbar.addEventListener("click", async () => {
      if (!selectedRun || typeof selectedRun.session_id !== "string") return;
      mainbar.disabled = true;
      const result = await dashboard.locateReply({
        session_id: selectedRun.session_id,
        run_id: selected,
        process_instance_id: connection,
        action_id: crypto.randomUUID(),
      });
      if (!closed) {
        locationNotice.textContent =
          result.status === "applied"
            ? "已在主对话中定位。"
            : result.status === "retired"
              ? "定位已退休；当前阅读位置保留。"
              : "正文定位未完成；可重试。";
        paintAction();
      }
    });
  }
  function captureSource() {
    return selected === null
      ? {
          route: url.pathname + url.search,
          kind: "runs",
          filter,
          anchor,
          anchorKind: "run",
          process_instance_id: connection,
          observed_at: reader.observed,
        }
      : { returnSource: origin };
  }
  /** @param {Wire} run */
  function row(run) {
    const result = runTableRow(run),
      link = node("a", "查看运行");
    link.href =
      "/runs/" +
      encodeURIComponent(run.run_id) +
      "?" +
      new URLSearchParams({ filter });
    follow(link, () => {
      anchor = run.run_id;
      void dashboard.navigate(link.href, {
        source: { returnSource: captureSource() },
      });
    });
    result.lastElementChild?.append(link);
    return result;
  }
  const reader = sourceReader(
    controls,
    "/api/runs",
    { filter, limit: "25" },
    (body, replace) => {
      if (
        !Array.isArray(body.runs) ||
        body.runs.some(
          (/** @type {Wire} */ run) => typeof run.run_id !== "string",
        )
      )
        throw new Error("source_identity_missing");
      if (replace) {
        finished.replaceChildren();
        seen.clear();
      }
      if (body.non_terminal) {
        const active = runtime().active;
        if (active?.run_id === body.non_terminal.run_id)
          sync({ ...body.non_terminal, ...active });
        else if (body.state_revision === undefined) sync(body.non_terminal);
      }
      for (const run of body.runs) {
        if (seen.has(run.run_id) || live?.run_id === run.run_id) continue;
        seen.add(run.run_id);
        finished.append(row(run));
      }
      if (!seen.size && !live) {
        const empty = node("tr"),
          cell = node("td", "此筛选下暂无运行。");
        cell.colSpan = 7;
        empty.append(cell);
        finished.append(empty);
      }
      if (body.target && body.source_focus_allowed)
        queueMicrotask(() => {
          if (!closed) highlightSource(list, body.target.anchor, "run");
        });
    },
    "运行",
    runtime,
  );
  /** @type {Map<string,Wire>} */ const expanded = new Map();
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
    if (closed || expanded.get(key) !== entry || result.state === "stale")
      return;
    if (result.state === "ok") {
      entry.state = "current";
      entry.record = result.body.record;
    } else if (result.state === "failed") {
      entry.state = result.status === 404 ? "gone" : "failed";
      entry.code = result.code;
    } else entry.state = "offline";
    update();
  }
  const references = {
    expanded,
    toggle(/** @type {Wire} */ ref) {
      const key = [ref.kind, ref.memory_id, ref.record_version].join("\u0000");
      if (expanded.delete(key)) update();
      else {
        expanded.set(key, { ref });
        void current(key);
      }
    },
  };
  const unwatch = memory.watch(() => {
    if (!closed) for (const key of expanded.keys()) void current(key);
  });
  function paintAction() {
    const available =
      selectedRun &&
      ["chat", "aggregation"].includes(selectedRun.purpose) &&
      typeof selectedRun.session_id === "string" &&
      selectedRun.admission_state === "admitted";
    mainbar.hidden = !available;
    const noReply =
      selectedRun?.reply_disposition === "no_reply" ||
      selectedRun?.aggregation?.reply_disposition === "no_reply" ||
      evidence?.memory?.routing?.reply_disposition === "no_reply";
    if (noReply) {
      mainbar.hidden = true;
      if (!summary.querySelector("[data-session-link]")) {
        const link = node("a", "查看所属会话");
        link.dataset.sessionLink = "true";
        link.href =
          "/inbox?" +
          new URLSearchParams({
            session_id: selectedRun?.session_id,
            view: "messages",
          });
        summary.append(link);
      }
    }
    mainbar.textContent =
      runtime().session === selectedRun?.session_id
        ? "在主对话中查看"
        : "切换到此会话并在主对话中查看";
    mainbar.disabled =
      runtime().connected === false || runtime().instance !== connection;
  }
  /** @param {Wire|null} active */
  function sync(active) {
    if (closed) return;
    if (selected !== null) {
      if (active?.run_id === selected) {
        selectedRun = { ...selectedRun, ...active };
        update();
      }
      paintAction();
      return;
    }
    const category =
      active?.filter ||
      (["chat", "aggregation"].includes(active?.purpose) ? "chat" : "system");
    if (filter !== "all" && filter !== category) active = null;
    const previous = live?.run_id;
    if (active && previous === active.run_id) active = { ...live, ...active };
    live = active;
    pinned.replaceChildren();
    if (active) {
      const card = row({ ...active, filter: category });
      card.dataset.pinned = "true";
      pinned.append(card);
      for (const old of finished.querySelectorAll("[data-source-id]"))
        if (old.getAttribute("data-source-id") === active.run_id) old.remove();
    }
    if (previous !== undefined && previous !== active?.run_id)
      notice.textContent = "当前运行槽已释放或变化；有新数据，请刷新运行列表。";
  }
  async function loadSelected() {
    if (
      closed ||
      runtime().connected !== true ||
      runtime().instance !== connection
    )
      return;
    const own = ++sequence;
    controller.abort();
    controller = new AbortController();
    notice.textContent = "读取运行…";
    metadataBusy = true;
    evidenceBusy = false;
    paintReadControls();
    try {
      const response = await fetch(
        "/api/runs/locate/" +
          encodeURIComponent(selected ?? "") +
          "?" +
          new URLSearchParams({ limit: "25", process_instance_id: connection }),
        { signal: controller.signal },
      );
      const body = await response.json();
      if (
        closed ||
        own !== sequence ||
        runtime().instance !== connection ||
        runtime().connected !== true
      )
        return;
      if (!response.ok) throw new Error(body.code || "运行不存在或暂不可读取");
      const next =
        [body.non_terminal, ...body.runs].find(
          (run) => run?.run_id === selected,
        ) || null;
      if (!next) throw new Error("目标运行不可读取");
      const active = runtime().active;
      selectedRun = active?.run_id === selected ? { ...next, ...active } : next;
      metadataAt = Date.now();
      metadataInvalidated = "";
      paintTimes();
      notice.textContent = "";
      update();
      void loadEvidence();
    } catch (error) {
      if (!closed && own === sequence) {
        notice.textContent =
          (error instanceof Error ? error.message : "运行暂不可读取") +
          "；已读摘要保留。";
        paintAction();
      }
    } finally {
      if (!closed && own === sequence) {
        metadataBusy = false;
        paintReadControls();
      }
    }
  }
  async function loadEvidence() {
    if (
      selected === null ||
      closed ||
      evidenceBusy ||
      runtime().connected !== true ||
      runtime().instance !== connection
    )
      return;
    evidenceBusy = true;
    paintReadControls();
    const own = sequence;
    try {
      const response = await fetch(
        "/api/run-evidence?" +
          new URLSearchParams({
            run_id: selected,
            ...(context.has("snapshot_id")
              ? { snapshot_id: context.get("snapshot_id") || "" }
              : {}),
          }),
        { signal: controller.signal },
      );
      const body = await response.json();
      if (
        closed ||
        own !== sequence ||
        runtime().instance !== connection ||
        runtime().connected !== true
      )
        return;
      if (response.status === 410 && body.error?.code === "snapshot_expired") {
        evidence = { snapshot_expired: true };
        evidenceError = "";
      } else {
        if (!response.ok || body.run_id !== selected)
          throw new Error("过程证据暂不可读取");
        if (
          evidence?.recording_state === "recorded" &&
          body.recording_state === "pending"
        )
          body.recording_state = "recorded";
        evidence = body;
        evidenceError = "";
        evidenceAt = Date.now();
        evidenceInvalidated = "";
      }
      evidenceVersion++;
    } catch {
      if (!closed && own === sequence)
        evidenceError =
          "过程证据读取失败；保留上次快照，不能据此确认当前状态。";
    } finally {
      if (!closed && own === sequence) {
        evidenceBusy = false;
        paintReadControls();
        update();
      }
    }
  }
  function update() {
    if (!selectedRun || closed || !root.isConnected) return;
    // Other Runs and disposable token deltas do not replace this reader's DOM.
    const signature = JSON.stringify([
      selectedRun,
      evidenceVersion,
      evidenceError,
      progress.events.get(selectedRun.run_id)?.size || 0,
      [...progress.attempts.values()]
        .filter((item) => item.run_id === selectedRun?.run_id)
        .map((item) => [item.attempt_id, item.dispatched]),
      [...expanded].map(([key, entry]) => [
        key,
        entry.state,
        entry.code,
        entry.record?.record_version,
      ]),
    ]);
    if (signature === lastRender) {
      paintAction();
      return;
    }
    lastRender = signature;
    if (!exportView && selected !== null)
      exportView = traceExport(exportMount, selected, csrf, memory);
    summary.replaceChildren(
      node("h2", "运行摘要"),
      runRow({
        ...selectedRun,
        recording_state: [
          selectedRun.recording_state,
          evidence?.recording_state,
        ].includes("recorded")
          ? "recorded"
          : [selectedRun.recording_state, evidence?.recording_state].includes(
                "failed",
              )
            ? "failed"
            : evidence?.recording_state || selectedRun.recording_state,
      }),
    );
    summary.dataset.highlighted = "true";
    paintTimes();
    const prior = new Map(
      [...detail.querySelectorAll("details")].map((item) => [
        item.dataset.attempt,
        { open: item.open, aborted: item.classList.contains("aborted") },
      ]),
    );
    const textOpen = new Set(
      [
        ...detail.querySelectorAll('[data-text-toggle][aria-expanded="true"]'),
      ].map((item) => item.getAttribute("data-text-toggle")),
    );
    const focused = document.activeElement;
    const focusAttempt =
      focused?.tagName === "SUMMARY" && detail.contains(focused)
        ? /** @type {HTMLElement} */ (focused.parentElement).dataset.attempt
        : null;
    const focusReference =
      focused instanceof HTMLElement && detail.contains(focused)
        ? focused.dataset.reference
        : undefined;
    const focusText =
      focused instanceof HTMLElement && detail.contains(focused)
        ? focused.dataset.textToggle
        : undefined;
    const restoreReading = captureReading(detail);
    closeAggregation();
    closeAggregation = () => {};
    detail.replaceChildren(node("h2", "过程证据"), node("p", "事件发布顺序"));
    if (evidenceError) detail.append(node("p", evidenceError));
    const memoryFacts = evidence?.memory;
    if (
      memoryFacts?.input_evidence_error ||
      memoryFacts?.input_failure ||
      memoryFacts?.input_preparation?.status === "failed"
    )
      detail.append(
        node("p", "输入准备失败或来源登记失败；该请求未发送，详情见本次输入。"),
      );
    if (memoryFacts?.input_unconfirmed?.length)
      detail.append(node("p", "输入登记待恢复；发送事实尚未确认。"));
    if (memoryFacts?.skills?.status === "failed")
      detail.append(node("p", "Skill 准备失败，详情见本次输入。"));
    if (evidence?.snapshot_expired) {
      usageBody.replaceChildren(
        node("p", "账目快照已失效，尚未核验当前调用账目。"),
      );
      const filters = new URLSearchParams({
        snapshot_context: context.has("ops_range") ? "expired" : "missing",
      });
      for (const key of [
        "range",
        "timezone",
        "start",
        "end",
        "session_id",
        "purpose",
        "tool",
        "run_id",
      ])
        if (context.has("ops_" + key))
          filters.set(key, context.get("ops_" + key) || "");
      const link = node("a", "返回账本核对筛选并刷新");
      link.href = "/ops?" + filters;
      detail.append(node("p", "账目快照已失效；尚未核验当前过程记录。"), link);
      paintAction();
      return;
    }
    if (evidence?.trace_incomplete) detail.append(node("p", "追踪不完整"));
    if (evidence && evidence.trace_status !== "available")
      detail.append(
        node(
          "p",
          /** @type {Record<string,string>} */ ({
            live: "实时过程由 SSE 提供",
            pruned: "过程记录已裁剪",
            partial: "过程记录不完整",
            too_large: "过程记录超过读取上限，未加载",
          })[evidence.trace_status] || "过程记录不可用",
        ),
      );
    const events = new Map();
    for (const event of evidence?.events || []) events.set(event.seq, event);
    for (const [seq, event] of progress.events.get(selectedRun.run_id) || [])
      events.set(seq, event);
    const confirmed = new Set(
      [...progress.attempts.values()]
        .filter(
          (item) => item.run_id === selectedRun?.run_id && item.dispatched,
        )
        .map((item) => item.attempt_id),
    );
    const runFinished =
      selectedRun.phase === "finished" ||
      [...events.values()].some(
        (event) => event.payload.name === "run.finished",
      );
    const actual = new Set(
      [
        ...(evidence?.attempts || []),
        ...(evidence?.memory?.input_attempts || []),
      ].map((/** @type {Wire} */ item) => item.attempt_id),
    );
    for (const event of events.values())
      if (["attempt.committed", "attempt.aborted"].includes(event.payload.name))
        actual.add(event.envelope.attempt_id);
    for (const id of confirmed) actual.add(id);
    for (const id of actual) confirmed.add(id);
    const authoritative =
      evidence?.memory?.input_preparation &&
      evidence.trace_status !== "live" &&
      runFinished;
    renderEvidence(
      detail,
      [...events.values()].filter(
        (event) =>
          !authoritative ||
          !event.envelope.attempt_id ||
          actual.has(event.envelope.attempt_id),
      ),
      evidence?.attempts || [],
      runFinished ? { ...selectedRun, phase: "finished" } : selectedRun,
      confirmed,
      new Map(
        (evidence?.memory?.input_attempts || []).map(
          (/** @type {Wire} */ input) => [input.attempt_id, input.purpose],
        ),
      ),
    );
    usageBody.replaceChildren(
      node(
        "p",
        "与过程证据使用同一读取来源；缺失值不视为零，作废调用仍计入记录。",
      ),
    );
    if (!evidence?.attempts?.length)
      usageBody.append(node("p", "暂无可读调用账目；不代表未调用或免费。"));
    for (const account of evidence?.attempts || []) {
      const entry = node("section");
      entry.append(
        node(
          "h3",
          `调用 ${account.attempt_id} · ${account.outcome || "结果未知"}`,
        ),
      );
      renderAccounting(entry, account);
      usageBody.append(entry);
    }
    renderInputs(
      detail,
      evidence?.memory,
      references,
      evidence?.trace_incomplete ? "partial" : evidence?.trace_status,
    );
    if (evidence?.memory?.aggregation)
      closeAggregation = aggregationFacts(
        detail,
        evidence.memory.aggregation,
        memory,
      );
    if (evidence?.memory?.routing) {
      const routing = evidence.memory.routing,
        section = node("section");
      section.append(
        node("h3", "消息分流"),
        node(
          "p",
          `${routing.route || "未执行图"} · ${routing.decision_reason || routing.fallback?.reason || ""}`,
        ),
        node(
          "p",
          `图结果：${routing.graph_result || "未执行"}；代际 ${routing.generation ?? "无"}`,
        ),
      );
      if (routing.reply_disposition === "no_reply")
        section.append(node("p", "已结束 · 按要求未回复"));
      if (routing.recoveries?.length) {
        section.append(node("p", "上下文准备失败，已降级处理。"));
        for (const recovery of routing.recoveries)
          section.append(
            node(
              "p",
              `${recovery.node_id} · ${recovery.code} · ${recovery.message} · ${recovery.side_effect_state}`,
            ),
          );
      }
      if (routing.classification?.actual_model)
        section.append(
          node(
            "p",
            `分类模型：${routing.classification.actual_model.endpoint_id} / ${routing.classification.actual_model.model_id}`,
          ),
        );
      if (routing.fallback?.decision !== "not_needed")
        section.append(
          node(
            "p",
            `普通回退：${routing.fallback?.decision} / ${routing.fallback?.reason}`,
          ),
        );
      detail.append(section);
    }
    for (const item of detail.querySelectorAll("details")) {
      const previous = prior.get(item.dataset.attempt);
      if (
        previous &&
        !(item.classList.contains("aborted") && !previous.aborted)
      )
        item.open = previous.open;
      if (focusAttempt === item.dataset.attempt)
        item.querySelector("summary")?.focus({ preventScroll: true });
    }
    for (const button of detail.querySelectorAll("button")) {
      if (textOpen.has(button.dataset.textToggle || null)) button.click();
      if (
        (focusReference !== undefined &&
          button.dataset.reference === focusReference) ||
        (focusText !== undefined && button.dataset.textToggle === focusText)
      )
        button.focus({ preventScroll: true });
    }
    restoreReading();
    paintAction();
  }
  function paintTimes() {
    const time = (/** @type {number} */ at) =>
      at
        ? `读取于 ${new Date(at).toLocaleTimeString()}${Date.now() - at >= 15 * 60 * 1000 ? " · 已过期，请刷新" : ""}`
        : "尚未读取";
    metadataStatus.textContent =
      "运行摘要 · " +
      time(metadataAt) +
      (metadataInvalidated ? " · " + metadataInvalidated : "");
    evidenceStatus.textContent =
      "过程证据共同来源 · " +
      time(evidenceAt) +
      (evidenceInvalidated ? " · " + evidenceInvalidated : "") +
      (evidenceError ? " · " + evidenceError : "");
  }
  const timer = setInterval(paintTimes, 60 * 1000);
  function paintReadControls() {
    const online = runtime().connected === true;
    refreshRun.disabled = metadataBusy || !online;
    refreshEvidence.disabled =
      evidenceBusy || !online || runtime().instance !== connection;
  }
  /** @param {string} reason */
  function retireReads(reason) {
    ++sequence;
    controller.abort();
    controller = new AbortController();
    if (metadataBusy) notice.textContent = reason;
    metadataBusy = evidenceBusy = false;
    metadataInvalidated = evidenceInvalidated = reason;
    paintTimes();
    paintReadControls();
  }
  let lastGap = 0;
  const unsubscribe = dashboard.subscribeState((/** @type {Wire} */ state) => {
    const projection = state.projection;
    if (
      selectedRun &&
      state.instance === connection &&
      projection?.run_id === selected &&
      projection.session_id === selectedRun.session_id
    ) {
      selectedRun = {
        ...selectedRun,
        recording_state: projection.recording_state,
        reply_disposition:
          projection.reply_disposition ?? selectedRun.reply_disposition,
      };
      update();
    }
    if (state.readGapRevision > lastGap) {
      lastGap = state.readGapRevision;
      reader.invalidate("增量通知缺口，快照待刷新");
      if (selected !== null) {
        retireReads("增量通知缺口，快照待刷新");
        evidenceError = "增量通知缺口，过程快照待刷新";
        paintTimes();
      }
    }
    if (state.instance && state.instance !== connection) {
      retireReads("进程已变化，旧快照仅供阅读");
      reader.invalidate("进程已变化，请重新打开此页面");
      notice.textContent = "进程已变化；旧快照仅供阅读，请重新打开此页面。";
      refreshRun.textContent = "重新核验运行";
    }
    if (state.connected === false) {
      reader.invalidate("连接中断，保留旧快照");
      retireReads("连接中断，保留旧快照");
    }
    reader.sync();
    paintReadControls();
    paintAction();
  });
  queueMicrotask(() => {
    if (closed || restored) return;
    if (selected === null) void reader.load();
    else void loadSelected();
  });
  return {
    sync,
    update,
    loadEvidence,
    captureSource,
    restoreSource(/** @type {Wire} */ source) {
      origin = source?.returnSource || null;
      if (selected !== null) {
        if (origin) {
          back.textContent = "返回来源";
          back.href = origin.route;
        }
        return;
      }
      if (source?.kind !== "runs" || typeof source.anchor !== "string") return;
      restored = true;
      anchor = source.anchor;
      if (source.process_instance_id !== connection) {
        reader.invalidate("来源进程已变化；请明确刷新当前列表");
        return;
      }
      void reader.load(true, {
        path: "/api/runs/locate/" + encodeURIComponent(source.anchor),
        params: { filter, limit: "25" },
      });
    },
    dispose() {
      closed = true;
      ++sequence;
      controller.abort();
      clearInterval(timer);
      closeAggregation();
      unwatch();
      unsubscribe();
      reader.close();
      pathView?.close();
      exportView?.dispose();
    },
  };
}
