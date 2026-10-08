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
  const fields = [
    ["Run ID", run.run_id],
    [
      "用途",
      decoded.value + (run.purpose_known === false ? "（未知用途）" : ""),
    ],
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
    ["阶段", run.phase || "未知"],
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
  row.dataset.sourceKey = "run";
  row.dataset.sourceId = run.run_id;
  const fields = runFields(run),
    groups = [
      ["Run ID", "用途", "所属分组", "会话"],
      ["输入预览", "聚合"],
      ["来源", "入口", "接受时间", "开始时间", "结束时间"],
      ["准入"],
      ["阶段", "结果"],
      ["记录", "记录来源"],
    ];
  for (const [index, labels] of groups.entries()) {
    const cell = node("td");
    cell.dataset.column = RUN_COLUMNS[index];
    const values = node("dl");
    for (const [label, value] of fields.filter(([label]) =>
      labels.includes(label),
    )) {
      const field = node("div");
      field.append(node("dt", label));
      const text = node("dd", value);
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
        field.append(text, button);
      } else field.append(text);
      values.append(field);
    }
    if (!values.childElementCount)
      values.append(node("dd", "按用途与 Run ID 识别"));
    cell.append(values);
    row.append(cell);
  }
  row.append(node("td"));
  return row;
}
