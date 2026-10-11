import { node } from "./dom.js";
/** @typedef {Record<string,any>} Wire */
/** @param {Wire} run */
export function outcomeLabel(run) {
  if (run.reply_disposition === "no_reply") return "已结束 · 按要求未回复";
  const labels = /** @type {Record<string,string>} */ ({
    completed: run.purpose === "chat" ? "回复完成" : "已完成",
    max_steps: "受控停止",
    failed: "受控失败",
    interrupted: run.started_at === null ? "执行前中断" : "运行终态无法确认",
  });
  if (
    (run.phase === "finished" && !run.outcome) ||
    (run.outcome && run.phase && run.phase !== "finished")
  )
    return "状态待核验";
  return (
    labels[run.outcome] ||
    (run.phase === "accepted"
      ? "已接受"
      : run.phase === "running"
        ? "运行中"
        : "结果未知")
  );
}
/** @param {Wire} run */
export function recordingLabel(run) {
  return (
    /** @type {Record<string,string>} */ ({
      recorded: "已保存",
      pending: "正在保存",
      failed: "未保存",
    })[run.recording_state] || "记录状态未知"
  );
}
/** @param {Wire} run */
export function runFields(run) {
  const decoded = node("textarea");
  decoded.innerHTML = run.purpose || "";
  const purpose = /** @type {Record<string,string>} */ ({
    chat: "普通聊天（chat）",
    aggregation: "手动聚合（aggregation）",
    inference_probe: "推理探针（inference_probe）",
    consolidation: "记忆提炼（consolidation）",
  });
  const fields = [
    ["Run ID", run.run_id],
    ["用途", purpose[run.purpose] || decoded.value + "（未知用途）"],
    ["所属分组", run.filter === "chat" ? "会话运行" : "系统运行"],
    ["来源", run.gateway ?? "未知"],
    ["入口", run.entry_surface_id ?? "未知"],
    ["会话", run.session_id === null ? "无会话" : (run.session_id ?? "未知")],
    ["接受时间", run.accepted_at ?? "未知"],
    ["开始时间", run.started_at ?? "尚未开始"],
    ["结束时间", run.finished_at ?? "尚未结束"],
    [
      "准入",
      /** @type {Record<string,string>} */ ({
        admitted: "已获准",
        pending: "准入处理中",
        rejected: "未获准",
        unconfirmed: "准入未确认",
      })[run.admission_state] || "准入未确认",
    ],
    [
      "阶段",
      /** @type {Record<string,string>} */ ({
        accepted: "已接受",
        running: "运行中",
        finished: "已结束",
      })[run.phase] || "状态待核验",
    ],
    ["结果", outcomeLabel(run)],
    ["记录", recordingLabel(run)],
    ["记录来源", run.recording_source || "未知"],
  ];
  if (run.admission_state === "admitted" && run.purpose === "chat")
    fields.push([
      "输入预览",
      typeof run.prompt_preview === "string"
        ? run.prompt_preview
        : "输入预览不可用",
    ]);
  if (run.purpose === "aggregation")
    fields.push([
      "聚合",
      run.aggregation
        ? `Graph ${run.aggregation.graph_result ?? "未知"} · ${run.aggregation.reply_disposition === "no_reply" ? "无草稿" : run.aggregation.reply_disposition === "reply" ? "有草稿" : "草稿状态未知"} · ${run.aggregation.reason_code ?? ""}`
        : "聚合状态未知",
    ]);
  return fields;
}
/** @param {Wire} run */
export function runRow(run) {
  const row = node("article");
  row.className = "run-row";
  row.dataset.sourceKey = "run";
  row.dataset.sourceId = run.run_id;
  const fields = node("dl");
  fields.className = "run-fields";
  for (const [label, value] of runFields(run)) {
    const field = node("div");
    field.append(node("dt", label), node("dd", value));
    fields.append(field);
  }
  row.append(fields);
  return row;
}

/** @param {Wire} run @param {Wire|null} [evidence] */
function runMetrics(run, evidence = null) {
  const finished = evidence?.events?.find(
    (/** @type {Wire} */ event) => event.payload.name === "run.finished",
  );
  const steps = finished?.payload.step_count;
  const observed = new Set(
    (evidence?.events || [])
      .map((/** @type {Wire} */ event) => event.envelope.step_index)
      .filter((/** @type {any} */ index) => Number.isInteger(index) && index >= 0),
  ).size;
  const started = Date.parse(run.started_at),
    ended = Date.parse(run.finished_at);
  return [
    [
      "Step 数",
      Number.isInteger(steps) && steps >= 0
        ? String(steps)
        : observed
          ? `已读 ${observed} · 总数未提供`
          : "未提供",
    ],
    [
      "耗时",
      Number.isFinite(started) && Number.isFinite(ended) && ended >= started
        ? `${((ended - started) / 1000).toFixed(1)} s`
        : "未提供",
    ],
  ];
}

/** @param {Wire} run @param {Wire|null} evidence */
export function runSummary(run, evidence) {
  const row = runRow(run);
  row.classList.add("run-summary");
  const lead = node("div"),
    message = node("dl"),
    identity = node("dl"),
    metadata = node("dl");
  lead.className = "run-summary-lead";
  message.className = "run-summary-message";
  identity.className = "run-summary-identity sy-row-meta";
  metadata.className = "run-summary-meta sy-row-meta";
  for (const field of row.querySelectorAll(".run-fields > div")) {
    const label = field.querySelector("dt")?.textContent;
    if (label === "输入预览") {
      message.append(field);
    } else if (["结果", "记录", "准入", "阶段"].includes(label || "")) metadata.append(field);
    else if (["Run ID", "用途", "所属分组"].includes(label || "")) identity.append(field);
  }
  for (const [label, value] of runMetrics(run, evidence)) {
    const field = node("div");
    field.append(node("dt", label), node("dd", value));
    metadata.append(field);
  }
  const costs = node("div");
  costs.className = "run-summary-costs sy-row-meta";
  for (const [index, attempt] of (evidence?.attempts || []).entries()) {
    const cost = attempt.cost || { state: "unknown" };
    const charge = node(
      "span",
      `调用 ${index + 1} · ${cost.state === "unknown" ? "unknown · 费用未知" : `${cost.state} · USD ${cost.amount}`}`,
    );
    charge.title = `Attempt ${attempt.attempt_id}`;
    charge.className = cost.state === "unknown" ? "sy-tone-attention" : "sy-tone-success";
    if (cost.state === "estimated") {
      for (const component of cost.price_components || [])
        charge.append(
          node(
            "small",
            ` · ${component.dimension}: ${component.price_source || component.source}${component.stale ? " · stale" : ""}`,
          ),
        );
      if (cost.price_components?.some((/** @type {Wire} */ component) => component.tiered))
        charge.append(node("small", " · 按基础档估算"));
    }
    costs.append(charge);
  }
  if (!costs.childElementCount) costs.append(node("span", "unknown · 费用未知"));
  if (!message.childElementCount) {
    const purpose = [...identity.children].find(
      (field) => field.querySelector("dt")?.textContent === "用途",
    );
    if (purpose) message.append(purpose);
  }
  const charges = node("div"),
    values = node("dd");
  values.append(costs);
  charges.append(node("dt", "费用"), values);
  metadata.append(charges);
  lead.append(message, metadata);
  const context = node("details"),
    fields = row.querySelector(".run-fields");
  context.className = "run-summary-context";
  context.append(node("summary", "运行摘要明细"));
  if (fields) context.append(fields);
  row.replaceChildren(lead, identity, context);
  return row;
}

export const RUN_COLUMNS = [
  "Run / 用途",
  "请求摘要",
  "入口 / 时间",
  "准入",
  "阶段 / 结局",
  "记录",
  "操作",
];
/** @param {Wire} run */
export function runTableRow(run) {
  const row = node("tr");
  row.className = "run-list-row sy-row";
  row.dataset.sourceKey = "run";
  row.dataset.sourceId = run.run_id;
  const cell = node("td"),
    preview = node("dl"),
    identity = node("dl"),
    metadata = node("dl"),
    context = node("details"),
    fields = node("dl");
  cell.colSpan = RUN_COLUMNS.length - 1;
  cell.className = "run-list-content sy-row-content";
  preview.className = "run-list-preview";
  identity.className = "run-list-identity sy-row-meta";
  metadata.className = "run-list-meta sy-row-meta";
  context.className = "run-list-context";
  context.append(node("summary", "运行记录明细"), fields);
  for (const [label, value] of [...runFields(run), ...runMetrics(run)]) {
    const field = node("div"),
      text = node("dd", value);
    field.append(node("dt", label), text);
    if (label === "输入预览" || label === "聚合") {
      if (label === "输入预览") {
        text.className = "run-preview";
        const button = node("button", "展开预览");
        button.setAttribute("aria-expanded", "false");
        button.addEventListener("click", () => {
          const open = button.getAttribute("aria-expanded") !== "true";
          button.setAttribute("aria-expanded", String(open));
          button.textContent = open ? "收起预览" : "展开预览";
          text.classList.toggle("expanded", open);
        });
        field.append(button);
      }
      preview.append(field);
    } else if (["Run ID", "用途", "所属分组"].includes(label)) identity.append(field);
    else if (["结果", "记录", "Step 数", "耗时"].includes(label)) metadata.append(field);
    else fields.append(field);
  }
  if (!preview.childElementCount)
    preview.append(node("dd", "按用途与 Run ID 识别"));
  cell.append(preview, metadata, identity, context);
  const actions = node("td");
  actions.className = "run-list-actions";
  row.append(cell, actions);
  return row;
}
