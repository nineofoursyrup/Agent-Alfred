import { node, textBlocks } from "./dom.js";
import { clampedText } from "./source.js";
import { interval, originLabel, sourceGroups } from "./memory.js";
/** @typedef {Record<string,any>} Wire */
/** @typedef {{expanded: Map<string, Wire>, toggle: (ref: Wire) => void}} References */
/** @param {HTMLElement} root @param {Wire[]} events @param {Wire[]} ledger @param {Wire} run @param {Set<string>} confirmed @param {Map<string,string>} purposes */
export function renderEvidence(root, events, ledger, run, confirmed, purposes) {
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
    const preparation = facts.find(
      (fact) => fact.payload.name === "step.started",
    );
    const skillSystem =
      preparation?.payload.skill_system ||
      (preparation?.payload.system || [])
        .filter(
          (/** @type {Wire} */ block) =>
            block.type === "text" && block.text?.startsWith("<skills>\n"),
        )
        .map((/** @type {Wire} */ block) => block.text);
    if (skillSystem.length) {
      const prepared = node("details");
      prepared.dataset.attempt = `skill-system-${index}`;
      prepared.append(
        node("summary", "准备时的 Skill 区段（脱敏追踪，不单独证明发送）"),
      );
      for (const [part, text] of skillSystem.entries())
        clampedText(prepared, text, `skill-${index}-${part}`);
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
    // The committed content appears once, in its Attempt below.
    const anchors = facts.filter(
      (fact) => fact.payload.name === "attempt.started",
    );
    for (const fact of facts) {
      if (
        ["attempt.committed", "attempt.aborted"].includes(fact.payload.name) &&
        !anchors.some(
          (anchor) => anchor.envelope.attempt_id === fact.envelope.attempt_id,
        )
      )
        anchors.push(fact);
    }
    for (const anchor of anchors.sort((a, b) => a.seq - b.seq)) {
      const id = anchor.envelope.attempt_id;
      const transitions = facts.filter(
        (fact) => fact.envelope.attempt_id === id,
      );
      const start = transitions.find(
        (fact) => fact.payload.name === "attempt.started",
      );
      const terminal = transitions.find((fact) =>
        ["attempt.committed", "attempt.aborted"].includes(fact.payload.name),
      );
      const accounting = ledger.find((attempt) => attempt.attempt_id === id);
      const actual = Boolean(terminal || accounting || confirmed.has(id));
      const details = node("details");
      details.dataset.attempt = id;
      details.id = `attempt-${id}`;
      const aborted = terminal?.payload.name === "attempt.aborted";
      details.className = !actual
        ? "input-preparation"
        : aborted
          ? "attempt aborted"
          : "attempt";
      details.open = false;
      details.append(
        node(
          "summary",
          actual
            ? `Attempt · seq ${anchor.seq} · ${aborted ? "aborted（已撤回）" : terminal ? "committed" : run.phase === "finished" ? "结果未知" : "运行中"}`
            : `输入准备 · seq ${anchor.seq} · 发送待确认`,
        ),
      );
      if (aborted) details.append(node("p", "未进入回答但已产生 Token／费用"));
      const purpose = purposes.get(id);
      details.append(
        node(
          "p",
          `调用用途：${purpose || "未知"}${start?.envelope.node_id ? " · 节点 " + start.envelope.node_id : ""}`,
        ),
      );
      if (start?.payload.model)
        details.append(
          node(
            "p",
            `${start.payload.model.endpoint_id} / ${start.payload.model.model_id}`,
          ),
        );
      const streamed = start?.payload.streamed;
      details.append(
        node(
          "p",
          streamed === true
            ? "流式"
            : streamed === false
              ? "非流式"
              : "流式状态未知",
        ),
      );
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
        clampedText(details, textBlocks(payload.blocks), "attempt-text-" + id);
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
  const unplaced = ledger.filter(
    (attempt) => !shownAccounts.has(attempt.attempt_id),
  );
  if (unplaced.length) {
    const accounting = node("section");
    accounting.append(node("h3", "调用账目 · 过程顺序不可用"));
    for (const attempt of unplaced) {
      const row = node("article");
      row.append(
        node("h4", `Attempt ${attempt.attempt_id} · ${attempt.outcome}`),
      );
      renderAccounting(row, attempt);
      accounting.append(row);
    }
    root.append(accounting);
  }
}
/** @param {HTMLElement} root @param {Wire|undefined} accounting */
export function renderAccounting(root, accounting) {
  const usage = accounting?.usage || {};
  for (const key of [
    "total_input_tokens",
    "uncached_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
  ])
    root.append(node("small", `${key}: ${usage[key] ?? "未提供"} `));
  renderCost(root, accounting?.cost || { state: "unknown" });
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
  for (const [name, label] of [
    ["semantic", "语义库"],
    ["episodic", "情景库"],
  ]) {
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
export function renderInputs(root, memory, references, traceStatus) {
  const details = node("details");
  details.dataset.attempt = "input-explanation";
  details.append(node("summary", "本次输入"));
  details.append(
    node("p", "字符限额是本地输入检查，不保证供应商 Token 容量。"),
  );
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
    details.append(
      node(
        "p",
        "输入来源或读取登记暂不可确认；该请求未发送。请检查存储状态后重试。",
      ),
    );
  }
  for (const pending of memory?.input_unconfirmed || []) {
    details.append(
      node(
        "p",
        `输入登记待恢复 · ${pending.purpose} · Step ${pending.step_index} · 请求 ${pending.attempt_id}；来源登记未确认，不计作已确认输入。`,
      ),
    );
  }
  const skills = memory?.skills;
  if (skills) {
    details.append(node("h3", "Skill"));
    details.append(
      node(
        "p",
        `选择方式：${skills.mode} · ${skills.status === "failed" ? "准备失败" : skills.status === "prepared" ? "准备完成，未单独证明发送" : "准备未完成"}`,
      ),
    );
    details.append(
      node("p", `所选顺序：${skills.selected?.join(" → ") || "无"}`),
    );
    if (skills.reason) details.append(node("p", `选择说明：${skills.reason}`));
    for (const skill of skills.loaded || [])
      details.append(
        node(
          "p",
          `${skill.name} · ${skill.source} · ${skill.overrides_builtin ? "整体覆盖内置" : "未覆盖"} · ${skill.codepoints} 码点 · ${skill.fingerprint_version} ${skill.body_sha256}`,
        ),
      );
    for (const excluded of skills.excluded || [])
      details.append(node("p", `未加载 ${excluded.name}：${excluded.reason}`));
    if (skills.loaded?.length)
      details.append(
        node(
          "p",
          traceStatus === "available"
            ? "当时正文请展开对应 Step 的脱敏追踪；当前目录不回填历史版本。"
            : "当时 Skill 正文追踪不可用或不完整；当前目录不能证明历史正文。",
        ),
      );
  } else details.append(node("p", "Skill 选择证据未知或不可用"));
  const selector = memory?.skill_input_preparation;
  if (selector)
    details.append(
      node(
        "p",
        `选择器输入 ${selector.status} · ${selector.characters} / ${selector.limit} 字符 · 历史预算排除 ${selector.budget_omitted_groups} 组`,
      ),
    );
  const preparation = memory?.input_preparation;
  if (preparation && preparation.purpose !== "skill_selector") {
    details.append(
      node(
        "p",
        `${preparation.status === "failed" ? "输入准备失败" : "容量选择"} · ${preparation.measurement_version}`,
      ),
    );
    details.append(
      node(
        "p",
        `回答已有 ${preparation.answer_characters} 字符，预留 ${preparation.reserved_characters} 字符，上限 ${preparation.answer_limit}；检索门 ${preparation.gate_characters} / ${preparation.gate_limit}。预留不计作实际发送。`,
      ),
    );
    const excluded = preparation.history_exclusions;
    details.append(
      node(
        "p",
        `准备阶段历史排除：不完整 ${excluded?.incomplete ?? "未知"}，隔离或来源未确认 ${excluded?.unsafe ?? "未知"}，N 上限 ${excluded?.round_limit ?? "未知"}，字符预算 ${preparation.budget_omitted_groups ?? "未知"}。`,
      ),
    );
    details.append(
      node(
        "p",
        `准备阶段工具账因限额省略 ${preparation.ledger_omitted ?? "未知"} 条，其中结果未知 ${preparation.ledger_unknown_omitted ?? "未知"} 条；隔离或失效排除 ${preparation.ledger_excluded ?? "未知"} 条。容量选择不计作实际发送，有限摘要不能证明动作未发生。`,
      ),
    );
  }
  if (memory?.input_failure) {
    const failure = memory.input_failure;
    details.append(
      node(
        "p",
        `输入准备失败 · ${failure.purpose === "skill_selector" ? "Skill 选择器" : `Step ${failure.step_index ?? "未知"}`} · ${failure.characters} 字符 / 上限 ${failure.limit}；未发送该请求。`,
      ),
    );
    const excluded = failure.history_exclusions;
    details.append(
      node(
        "p",
        `本次失败准备历史排除：不完整 ${excluded?.incomplete ?? "未知"}，隔离或来源未确认 ${excluded?.unsafe ?? "未知"}，N 上限 ${excluded?.round_limit ?? "未知"}，字符预算 ${failure.budget_omitted_groups ?? "未知"}。`,
      ),
    );
    if (failure.purpose !== "skill_selector")
      details.append(
        node(
          "p",
          `本次失败准备工具账因限额省略 ${failure.ledger_omitted ?? "未知"} 条，其中结果未知 ${failure.ledger_unknown_omitted ?? "未知"} 条；隔离或失效排除 ${failure.ledger_excluded ?? "未知"} 条。不计作实际发送。`,
        ),
      );
  }
  for (const input of memory?.input_attempts || []) {
    const section = node("section");
    section.append(
      node(
        "h3",
        `${input.purpose} · Step ${input.step_index} · Attempt ${input.attempt_id}`,
      ),
    );
    section.append(
      node(
        "p",
        `${input.measurement_version ?? "计量未知"} · ${input.input_characters ?? "未知"} 字符 / 上限 ${input.input_limit ?? "未知"}`,
      ),
    );
    const omitted = input.history_exclusions;
    if (omitted)
      section.append(
        node(
          "p",
          `历史排除：不完整 ${omitted.incomplete}，隔离或来源未确认 ${omitted.unsafe}，N 上限 ${omitted.round_limit}，字符预算 ${input.budget_omitted_groups}。`,
        ),
      );
    for (const skill of input.skills || [])
      section.append(
        node(
          "p",
          `实际请求携带 Skill ${skill.name} · ${skill.source} · ${skill.fingerprint_version} ${skill.body_sha256}`,
        ),
      );
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
      section.append(
        node(
          "p",
          `工具账 ${entry.ledger_id} · ${entry.action} · ${entry.status} · ${entry.at} · 来源 Run ${entry.source_run}`,
        ),
      );
    }
    if (input.purpose === "answer")
      section.append(
        node(
          "p",
          `工具账因限额省略 ${input.ledger_omitted ?? "未知"} 条，其中结果未知 ${input.ledger_unknown_omitted ?? "未知"} 条；隔离或失效排除 ${input.ledger_excluded ?? "未知"} 条。有限摘要不能证明动作未发生。`,
        ),
      );
    details.append(section);
  }
  root.append(details);
}
