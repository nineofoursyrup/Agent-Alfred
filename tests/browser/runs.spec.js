import { test, expect } from "@playwright/test";
import { controlledTransport, domain, emit, state, run } from "./transport.js";

test("input source failure stays explicit after reloading run details", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  await page.getByRole("textbox", { name: "消息" }).fill("输入来源失败展示");
  const accepted = page.waitForResponse(response =>
    response.url().endsWith("/api/runs") && response.status() === 202);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const { run_id } = await (await accepted).json();
  await expect(page.getByRole("region", { name: "主对话" })).toContainText("已保存");
  await page.route("**/api/run-evidence?*", async route => {
    const body = await (await route.fetch()).json();
    await route.fulfill({json: {...body, events: [], attempts: [], memory: {
      gate_state: "not_evaluated", gate: null, input_attempts: [],
      input_evidence_error: "input_evidence_unavailable",
    }}});
  });
  await page.goto(`/runs/${encodeURIComponent(run_id)}`);
  await page.getByText("本次输入", { exact: true }).click();
  const detail = page.getByRole("region", { name: "运行过程" });
  await expect(detail).toContainText("输入来源或读取登记暂不可确认");
  await expect(detail).toContainText("该请求未发送");
  await page.reload();
  await page.getByText("本次输入", { exact: true }).click();
  await expect(detail).toContainText("输入来源或读取登记暂不可确认");
});

test("later failed preparation displays its own final exclusions after reload", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  await page.getByRole("textbox", { name: "消息" }).fill("输入来源失败展示");
  const accepted = page.waitForResponse(response =>
    response.url().endsWith("/api/runs") && response.status() === 202);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const { run_id } = await (await accepted).json();
  await expect(page.getByRole("region", { name: "主对话" })).toContainText("已保存");
  await page.route("**/api/run-evidence?*", async route => {
    const body = await (await route.fetch()).json();
    await route.fulfill({json: {...body, events: [], attempts: [], memory: {
      gate_state: "not_evaluated", gate: null, input_attempts: [],
      input_preparation: {status: "prepared", budget_omitted_groups: 0},
      input_failure: {status: "failed", measurement_version: "request-input-v1",
        characters: 65000, limit: 64000, step_index: 3,
        history_exclusions: {incomplete: 0, unsafe: 0, round_limit: 0},
        budget_omitted_groups: 1, ledger_omitted: 1,
        ledger_unknown_omitted: 0, ledger_excluded: 0},
    }}});
  });
  await page.goto(`/runs/${encodeURIComponent(run_id)}`);
  await page.getByText("本次输入", { exact: true }).click();
  const detail = page.getByRole("region", { name: "运行过程" });
  await expect(detail).toContainText("本次失败准备历史排除：不完整 0，隔离或来源未确认 0，N 上限 0，字符预算 1");
  await expect(detail).toContainText("未发送该请求");
  await expect(detail).toContainText("本次失败准备工具账因限额省略 1 条，其中结果未知 0 条");
  await page.reload();
  await page.getByText("本次输入", { exact: true }).click();
  await expect(detail).toContainText("本次失败准备历史排除：不完整 0，隔离或来源未确认 0，N 上限 0，字符预算 1");
});


test("unconfirmed input registration remains unknown after reload", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  await page.getByRole("textbox", { name: "消息" }).fill("输入来源失败展示");
  const accepted = page.waitForResponse(response =>
    response.url().endsWith("/api/runs") && response.status() === 202);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const { run_id } = await (await accepted).json();
  await expect(page.getByRole("region", { name: "主对话" })).toContainText("已保存");
  await page.route("**/api/run-evidence?*", async route => {
    const body = await (await route.fetch()).json();
    await route.fulfill({json: {...body, events: [], attempts: [], memory: {
      gate_state: "not_evaluated", gate: null, input_attempts: [],
      input_resolution_error: "input_resolution_unavailable",
      input_unconfirmed: [{attempt_id: "pending-input", purpose: "gate", step_index: 0}],
    }}});
  });
  await page.goto(`/runs/${encodeURIComponent(run_id)}`);
  await page.getByText("本次输入", { exact: true }).click();
  const detail = page.getByRole("region", { name: "运行过程" });
  await expect(detail).toContainText("输入登记待恢复");
  await expect(detail).toContainText("来源登记未确认，不计作已确认输入");
  await expect(detail).not.toContainText("该请求未发送");
  await page.reload();
  await page.getByText("本次输入", { exact: true }).click();
  await expect(detail).toContainText("输入登记待恢复");
});


test("oversized input shows preparation failure without an invented Attempt", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  await page.getByRole("textbox", { name: "消息" }).fill("先保存一轮历史");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("region", { name: "主对话" })).toContainText("已保存");
  await page.getByRole("textbox", { name: "消息" }).fill("/skills off\n" + "x".repeat(64001));
  const accepted = page.waitForResponse(response =>
    response.url().endsWith("/api/runs") && response.status() === 202);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const { run_id } = await (await accepted).json();
  await expect(page.getByRole("region", { name: "主对话" })).toContainText("已保存");
  await page.goto(`/runs/${encodeURIComponent(run_id)}`);
  await page.getByText("本次输入", { exact: true }).click();
  const detail = page.getByRole("region", { name: "运行过程" });
  await expect(detail).toContainText("输入准备失败");
  await expect(detail).toContainText("准备阶段历史排除：不完整 0，隔离或来源未确认 0，N 上限 0，字符预算 1");
  await expect(detail).toContainText("准备阶段工具账因限额省略 0 条，其中结果未知 0 条；隔离或失效排除 0 条");
  await expect(detail).toContainText("预留");
  await expect(detail).not.toContainText("Attempt");
  await page.reload();
  await page.getByText("本次输入", { exact: true }).click();
  await expect(detail).toContainText("输入准备失败");
  await expect(detail).toContainText("准备阶段历史排除：不完整 0，隔离或来源未确认 0，N 上限 0，字符预算 1");
  await expect(detail).toContainText("准备阶段工具账因限额省略 0 条，其中结果未知 0 条；隔离或失效排除 0 条");
});

test("preparation exclusions use server counts without manufacturing actual input", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  await page.getByRole("textbox", { name: "消息" }).fill("x".repeat(64001));
  const accepted = page.waitForResponse(response =>
    response.url().endsWith("/api/runs") && response.status() === 202);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const { run_id } = await (await accepted).json();
  await expect(page.getByRole("region", { name: "主对话" })).toContainText("已保存");
  await page.route("**/api/run-evidence?*", async route => {
    const body = await (await route.fetch()).json();
    body.memory.input_preparation = {
      ...body.memory.input_preparation,
      history_exclusions: { incomplete: 2, unsafe: 3, round_limit: 4 },
      budget_omitted_groups: 5, ledger_omitted: 7,
      ledger_unknown_omitted: 6, ledger_excluded: 8,
    };
    await route.fulfill({ json: body });
  });
  await page.goto(`/runs/${encodeURIComponent(run_id)}`);
  for (const reload of [false, true]) {
    if (reload) await page.reload();
    await page.getByText("本次输入", { exact: true }).click();
    const detail = page.getByRole("region", { name: "运行过程" });
    await expect(detail).toContainText("准备阶段历史排除：不完整 2，隔离或来源未确认 3，N 上限 4，字符预算 5");
    await expect(detail).toContainText("准备阶段工具账因限额省略 7 条，其中结果未知 6 条；隔离或失效排除 8 条");
    await expect(detail).toContainText("容量选择不计作实际发送");
    await expect(detail).not.toContainText("Attempt");
  }
});

test("a real recorded Run deep link loads published Attempt evidence and exact cost", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  await page.getByRole("textbox", { name: "消息" }).fill("运行详情测试");
  const accepted = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/runs") && response.status() === 202,
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const { run_id } = await (await accepted).json();
  await expect(page.getByRole("region", { name: "主对话" })).toContainText(
    "已保存",
  );
  await page.goto(`/runs/${encodeURIComponent(run_id)}?filter=system`);
  await expect(page.getByRole("combobox", { name: "运行筛选" })).toHaveValue(
    "chat",
  );
  const detail = page.getByRole("region", { name: "运行过程" });
  await expect(detail).toContainText("事件发布顺序");
  await expect(detail).toContainText("committed");
  await expect(detail).toContainText("exact");
  await expect(detail).toContainText("0.125");
  await expect(detail).not.toContainText("按基础档估算");
  await expect(detail).toContainText("离线模型回复");
  await detail.getByText("本次输入", { exact: true }).click();
  await expect(detail).toContainText("request-input-v1");
  await expect(detail).toContainText("字符 / 上限 64000");
  await expect(detail).toContainText("预留");
  await page.reload();
  await page.getByText("本次输入", { exact: true }).click();
  await expect(page.getByRole("region", { name: "运行过程" })).toContainText(
    "request-input-v1",
  );
  await expect(
    page.getByRole("main").locator('[data-highlighted="true"]'),
  ).toHaveCount(1);
});

test("a Run admitted after opening the list appears once as the pinned running row", async ({
  page,
}) => {
  const session = await controlledTransport(page);
  await page.route("**/api/runs?*", (route) =>
    route.fulfill({
      json: { filter: "all", runs: [], non_terminal: null, next_cursor: null },
    }),
  );
  await page.getByRole("link", { name: "运行", exact: true }).click();
  await emit(
    page,
    "state_patch",
    state(session, 1, {
      coordinator_state: "running",
      active_run: run(session),
    }),
  );
  const list = page.getByRole("region", { name: "运行列表" });
  await expect(
    list.getByRole("link", { name: "查看运行", exact: true }),
  ).toHaveCount(1);
  await expect(list).toContainText("运行中");
  await emit(
    page,
    "state_patch",
    state(session, 2, {
      coordinator_state: "running",
      active_run: run(session),
    }),
  );
  await expect(
    list.getByRole("link", { name: "查看运行", exact: true }),
  ).toHaveCount(1);
});

test("aborted attempts stay in publication position and unknown cost never has an amount", async ({
  page,
}) => {
  const run = {
    run_id: "system-run",
    purpose: "&lt;future-purpose&gt;",
    filter: "system",
    phase: "finished",
    outcome: "failed",
    gateway: "cli",
    accepted_at: "today",
    started_at: "today",
  };
  const fact = (seq, attempt, name, payload = {}) => ({
    seq,
    envelope: {
      run_id: run.run_id,
      step_index: 0,
      attempt_id: attempt,
      ts: 100 - seq,
    },
    payload: { name, ...payload },
  });
  await page.route("**/api/runs/locate/*", (route) =>
    route.fulfill({
      json: {
        filter: "system",
        runs: [run],
        non_terminal: null,
        next_cursor: null,
      },
    }),
  );
  await page.route("**/api/run-evidence?*", (route) =>
    route.fulfill({
      json: {
        run_id: run.run_id,
        trace_status: "available",
        trace_incomplete: true,
        recording_state: "recorded",
        attempts: [
          {
            attempt_id: "aborted",
            usage: { output_tokens: 3 },
            cost: { state: "unknown", amount: "999" },
          },
          {
            attempt_id: "good",
            usage: { output_tokens: 6 },
            cost: {
              state: "estimated",
              amount: "0.006",
              price_components: [
                { dimension: "output", price_source: "catalog", stale: true },
              ],
            },
          },
          {
            attempt_id: "tiered",
            usage: { output_tokens: 6 },
            cost: {
              state: "estimated",
              amount: "0.007",
              price_components: [
                {
                  dimension: "output",
                  price_source: "model_static",
                  tiered: true,
                },
              ],
            },
          },
        ],
        events: [
          fact(8, "good", "attempt.committed", {
            blocks: [
              { type: "text", text: "提交快照" },
              { type: "thinking", text: "不应显示的思考" },
              { type: "tool_call", input: { secret: "工具参数" } },
            ],
          }),
          fact(4, "aborted", "attempt.started"),
          fact(5, "aborted", "attempt.aborted", {
            blocks: [{ type: "text", text: "已作废片段" }],
          }),
          fact(6, "good", "attempt.started"),
          fact(10, "tiered", "attempt.committed", {
            blocks: [{ type: "text", text: "阶梯快照" }],
          }),
          fact(9, "tiered", "attempt.started"),
        ],
      },
    }),
  );
  await page.goto("/runs/system-run?filter=chat");
  await expect(page.getByRole("combobox", { name: "运行筛选" })).toHaveValue(
    "system",
  );
  await expect(page.getByRole("main")).toContainText("<future-purpose>");
  const detail = page.getByRole("region", { name: "运行过程" });
  const summaries = detail.locator("details.attempt > summary");
  await expect(summaries).toHaveText([
    "Attempt · seq 4 · aborted（已撤回）",
    "Attempt · seq 6 · committed",
    "Attempt · seq 9 · committed",
  ]);
  await expect(detail.getByText("已作废片段", { exact: true })).toBeHidden();
  await summaries.first().click();
  const aborted = detail.locator('details[data-attempt="aborted"]');
  const estimated = detail.locator('details[data-attempt="good"]');
  const tiered = detail.locator('details[data-attempt="tiered"]');
  await expect(aborted).toContainText("未进入回答但已产生 Token／费用");
  await expect(aborted).toContainText("unknown · 费用未知");
  await expect(aborted).not.toContainText("999");
  await expect(aborted).not.toContainText("按基础档估算");
  await expect(estimated).toContainText("estimated · USD 0.006");
  await expect(estimated).toContainText("output: catalog · stale");
  await expect(estimated).not.toContainText("按基础档估算");
  await expect(tiered).toContainText("estimated · USD 0.007");
  await expect(tiered).toContainText("output: model_static");
  await expect(tiered).toContainText("按基础档估算");
  await expect(detail).toContainText("thinking × 1");
  await expect(detail).not.toContainText("不应显示的思考");
  await expect(detail).not.toContainText("工具参数");
  await expect(detail).toContainText("追踪不完整");
  await expect(page.getByRole("main")).toContainText("受控失败");
});

test("durable accounting survives absent trace and event usage cannot manufacture a charge", async ({ page }) => {
  let missing = true;
  await page.route("**/api/runs/locate/*", route => route.fulfill({ json: {
    filter: "chat", runs: [{ run_id: "ledger", purpose: "chat", phase: "finished", outcome: "completed" }], next_cursor: null,
  }}));
  await page.route("**/api/run-evidence?*", route => route.fulfill({ json: {
    run_id: "ledger", trace_status: missing ? "unavailable" : "partial", recording_state: "recorded",
    attempts: missing ? [{ attempt_id: "durable", outcome: "committed", usage: { output_tokens: 42 }, cost: {state: "exact", amount: "0.42"} }] : [],
    events: missing ? [] : ["attempt.started", "attempt.committed"].map((name, i) => ({seq: i+1, envelope: {step_index: 0, attempt_id: "orphan"}, payload: {name, usage: {output_tokens: 999, endpoint_reported_cost_usd: "999"}, cost: {state: "exact", amount: "999"}}})),
  }}));
  await page.goto("/runs/ledger");
  const detail = page.getByRole("region", {name: "运行过程"});
  await expect(detail).toContainText("output_tokens: 42");
  await expect(detail).toContainText("exact · USD 0.42");
  await expect(detail).not.toContainText("按基础档估算");
  await expect(detail).toContainText("过程顺序不可用");
  missing = false;
  await page.reload();
  await expect(detail).toContainText("unknown · 费用未知");
  await expect(detail).not.toContainText("999");
});


test("live Attempt updates keep keyboard focus and collapse only the new abort", async ({page}) => {
  const session = await controlledTransport(page);
  await page.route("**/api/runs/locate/*", route => route.fulfill({json: {filter: "chat", runs: [], non_terminal: run(session), next_cursor: null}}));
  await page.route("**/api/run-evidence?*", route => route.fulfill({json: {run_id: "r1", trace_status: "live", events: [], attempts: []}}));
  await page.evaluate(() => {history.pushState(null, "", "/runs/r1"); window.dispatchEvent(new PopStateEvent("popstate"));});
  await emit(page, "state_patch", state(session, 1, {coordinator_state: "running", active_run: run(session)}));
  await domain(page, 1, session, {name: "attempt.started", attempt_id: "a"}, {attempt: "a", step: 0});
  const detail = page.getByRole("region", {name: "运行过程"});
  const summary = detail.locator('details[data-attempt="a"] > summary');
  await expect(summary).toContainText("发送待确认");
  await summary.focus();
  await domain(page, 2, session, {name: "block.started", index: 0, block_type: "text"}, {attempt: "a", step: 0});
  await expect(summary).toBeFocused();
  await expect(detail.locator("details.attempt > summary")).toContainText("运行中");
  await domain(page, 3, session, {name: "attempt.aborted", blocks: [{type: "text", text: "作废正文"}]}, {attempt: "a", step: 0});
  await expect(summary).toContainText("aborted");
  await expect(detail.locator("details.attempt")).not.toHaveAttribute("open", "");
  await expect(summary).toBeFocused();
  await summary.press("Enter");
  await expect(detail.getByText("作废正文", {exact:true})).toBeVisible();
  await domain(page, 4, session, {name: "step.finished"}, {step: 0});
  await expect(detail.getByText("作废正文", {exact:true})).toBeVisible();
  await expect(summary).toBeFocused();
});

test("global busy stays in MainBar while pinned Runs obey filters and deep-link correction", async ({page}) => {
  const session = await controlledTransport(page);
  await page.route("**/api/runs?*", route => route.fulfill({json:{filter:new URL(route.request().url()).searchParams.get("filter"), runs:[],next_cursor:null}}));
  await page.getByRole("link", {name:"运行",exact:true}).click();
  await page.getByRole("combobox", {name:"运行筛选"}).selectOption("chat");
  const list = page.getByRole("region", {name:"运行列表"});
  await emit(page, "state_patch", state(session, 1, {coordinator_state:"running",active_run:run(session,{purpose:"maintenance"})}));
  await expect(page.getByRole("region", {name:"当前运行"})).toContainText("运行中");
  await expect(list.getByRole("link", {name:"查看运行"})).toHaveCount(0);
  await page.getByRole("combobox", {name:"运行筛选"}).selectOption("system");
  await expect(list.getByRole("link", {name:"查看运行"})).toHaveCount(1);
  await emit(page, "state_patch", state(session, 2, {coordinator_state:"running",active_run:run(session)}));
  await expect(list.getByRole("link", {name:"查看运行"})).toHaveCount(0);
  await page.route("**/api/runs/locate/*", route => route.fulfill({json:{filter:"system", runs:[{run_id:"old-system",purpose:"maintenance",phase:"finished",outcome:"completed",filter:"system"}],next_cursor:null}}));
  await page.route("**/api/run-evidence?*", route => route.fulfill({json:{run_id:"old-system",trace_status:"unavailable",events:[],attempts:[]}}));
  await page.evaluate(() => {history.pushState(null,"","/runs/old-system?filter=chat");window.dispatchEvent(new PopStateEvent("popstate"));});
  await expect(page.getByRole("combobox", {name:"运行筛选"})).toHaveValue("system");
  await expect(list.getByRole("link", {name:"查看运行"})).toHaveCount(1);
  await expect(list).not.toContainText("运行中");
  await expect(page.getByRole("region", {name:"当前运行"})).toContainText("运行中");
});

test("unsent SSE preparation never becomes an actual Attempt", async ({page}) => {
  const session = await controlledTransport(page);
  let finished = false;
  await page.route("**/api/runs/locate/*", route => route.fulfill({json: {
    filter: "chat", runs: [], non_terminal: run(session), next_cursor: null,
  }}));
  await page.route("**/api/run-evidence?*", route => route.fulfill({json: {
    run_id: "r1", trace_status: finished ? "available" : "live",
    events: [], attempts: [], memory: {gate_state: "evaluated", input_attempts: [],
      input_preparation: {status: "prepared", measurement_version: "request-input-v1"}},
  }}));
  await page.evaluate(() => {history.pushState(null, "", "/runs/r1"); window.dispatchEvent(new PopStateEvent("popstate"));});
  await emit(page, "state_patch", state(session, 1, {coordinator_state: "running", active_run: run(session)}));
  await domain(page, 1, session, {name: "attempt.started", attempt_id: "unsent"}, {attempt: "unsent", step: 0});
  const detail = page.getByRole("region", {name: "运行过程"});
  await expect(detail.getByText("本次输入", {exact: true})).toBeVisible();
  await expect(detail.locator("details.attempt")).toHaveCount(0);
  finished = true;
  await domain(page, 2, session, {name: "step.finished"}, {step: 0});
  await domain(page, 3, session, {name: "run.finished", outcome: "max_steps"});
  await expect(detail.locator("details.attempt")).toHaveCount(0);
  await page.reload();
  const restored = page.getByRole("region", {name: "运行过程"});
  await expect(restored.getByText("本次输入", {exact: true})).toBeVisible();
  await emit(page, "state_patch", state(session, 2, {coordinator_state: "running", active_run: run(session)}));
  await domain(page, 4, session, {name: "attempt.started", attempt_id: "unsent"}, {attempt: "unsent", step: 0});
  await expect(restored.locator("details.attempt")).toHaveCount(0);
  await domain(page, 5, session, {name: "run.finished", outcome: "max_steps"});
  await expect(restored.locator("details.attempt")).toHaveCount(0);
});

for (const terminal of [false, true]) test(`confirmed input survives unavailable accounting (terminal=${terminal})`,async({page})=>{
 await page.route('**/api/runs/locate/*',route=>route.fulfill({json:{filter:'chat',runs:[{run_id:'confirmed',purpose:'chat',phase:'finished',outcome:'failed'}],next_cursor:null}}));
 await page.route('**/api/run-evidence?*',route=>route.fulfill({json:{run_id:'confirmed',trace_status:'partial',attempts:[],memory:{gate_state:'evaluated',input_preparation:{status:'prepared'},input_attempts:[{attempt_id:'real',purpose:'gate',step_index:0,input_characters:100,input_limit:64000}]},events:(terminal ? ['attempt.started','attempt.aborted'] : ['attempt.started']).map((name,i)=>({seq:i+1,envelope:{step_index:0,attempt_id:'real'},payload:{name}}))}}));
 await page.goto('/runs/confirmed');
 const detail=page.getByRole('region',{name:'运行过程'});
 await expect(detail.getByText('本次输入',{exact:true})).toBeVisible();
 await expect(detail.locator('details.attempt')).toHaveCount(1);
 if (!terminal) await expect(detail.locator('details.attempt > summary')).toContainText('结果未知');
});

for (const proof of ["committed", "aborted", "block", "start-only", "terminal-only"])
  test(`cached ${proof} evidence survives unavailable receipts correctly`, async ({page}) => {
    const session = await controlledTransport(page);
    let finished = false;
    let diskEvents = [];
    await page.route("**/api/runs/locate/*", route => route.fulfill({json: {
      filter: "chat", runs: [], non_terminal: run(session), next_cursor: null,
    }}));
    await page.route("**/api/run-evidence?*", route => route.fulfill({json: {
      run_id: "r1", trace_status: finished ? "available" : "live", events: diskEvents,
      attempts: [], memory: {gate_state: "incomplete", input_preparation: {status: "prepared"},
        input_attempts: [], input_unconfirmed: [{attempt_id: "real", purpose: "gate", step_index: 0}]},
    }}));
    await page.evaluate(() => {history.pushState(null, "", "/runs/r1"); window.dispatchEvent(new PopStateEvent("popstate"));});
    await emit(page, "state_patch", state(session, 1, {coordinator_state: "running", active_run: run(session)}));
    if (proof !== "terminal-only")
      await domain(page, 1, session, {name: "attempt.started", attempt_id: "real"}, {attempt: "real", step: 0});
    const terminal = {name: proof === "aborted" ? "attempt.aborted" : "attempt.committed",
      attempt_id: "real", blocks: [{type: "text", text: "real terminal fixture"}]};
    if (proof !== "start-only")
      await domain(page, 2, session, proof === "block"
        ? {name: "block.started", attempt_id: "real", index: 0, block_type: "text"}
        : terminal, {attempt: "real", step: 0});
    if (proof === "block")
      await domain(page, 3, session, {name: "block.delta", attempt_id: "real", index: 0,
        text: "ephemeral body"}, {attempt: "real", step: 0});
    const detail = page.getByRole("region", {name: "运行过程"});
    await expect(detail.locator("details.attempt")).toHaveCount(proof === "start-only" ? 0 : 1);
    finished = true;
    const refreshed = page.waitForResponse(r => r.url().includes("/api/run-evidence?"));
    await domain(page, 4, session, {name: "run.finished", outcome: "failed"});
    await refreshed;
    await expect(detail.locator("details.attempt")).toHaveCount(proof === "start-only" ? 0 : 1);
    if (proof === "block")
      await expect(detail.locator("details.attempt > summary")).toContainText("结果未知");
    // Only terminal snapshots can restore body/identity after a fresh connection.
    // A trace can retain a terminal even when its earlier start is unavailable.
    const persistent = !["block", "start-only"].includes(proof);
    if (persistent) diskEvents = [{seq: 2, envelope: {step_index: 0, attempt_id: "real"}, payload: terminal}];
    await page.reload();
    const restored = page.getByRole("region", {name: "运行过程"});
    await expect(restored.getByText("本次输入", {exact: true})).toBeVisible();
    await expect(restored.locator("details.attempt")).toHaveCount(persistent ? 1 : 0);
    await expect(restored).not.toContainText("ephemeral body");
    if (persistent) await expect(restored.locator("details.attempt")).toContainText("real terminal fixture");
  });

for (const mode of ["streamed", "nonstreamed", "missing", "terminal-only"])
  test(`stream mode uses actual start metadata (${mode})`, async ({page}) => {
    const events = [];
    if (mode !== "terminal-only") events.push({seq: 1,
      envelope: {step_index: 0, attempt_id: "real"}, payload: {name: "attempt.started",
        ...(mode === "missing" ? {} : {streamed: mode === "streamed"})},
    });
    events.push({seq: 2, envelope: {step_index: 0, attempt_id: "real"},
      payload: {name: "attempt.committed", blocks: [{type: "text", text: "saved terminal"}]},
    });
    await page.route("**/api/runs/locate/*", route => route.fulfill({json: {
      filter: "chat", runs: [{run_id: "stream-mode", purpose: "chat", phase: "finished", outcome: "completed"}],
      next_cursor: null,
    }}));
    await page.route("**/api/run-evidence?*", route => route.fulfill({json: {
      run_id: "stream-mode", trace_status: "available", events, attempts: [],
    }}));
    await page.goto("/runs/stream-mode");
    const expected = mode === "streamed" ? "流式" : mode === "nonstreamed" ? "非流式" : "流式状态未知";
    for (const reload of [false, true]) {
      if (reload) await page.reload();
      const attempt = page.getByRole("region", {name: "运行过程"}).locator("details.attempt");
      await expect(attempt).toHaveCount(1);
      await expect(attempt.getByText(expected, {exact: true})).toBeVisible();
      if (mode !== "nonstreamed")
        await expect(attempt.getByText("非流式", {exact: true})).toHaveCount(0);
    }
  });
