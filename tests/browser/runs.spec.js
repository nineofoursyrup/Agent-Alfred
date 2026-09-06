import { test, expect } from "@playwright/test";
import { controlledTransport, domain, emit, state, run } from "./transport.js";

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
  await expect(detail).toContainText("离线模型回复");
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
  const summaries = detail.locator("details > summary");
  await expect(summaries).toHaveText([
    "Attempt · seq 4 · aborted（已撤回）",
    "Attempt · seq 6 · committed",
  ]);
  await expect(detail.getByText("已作废片段", { exact: true })).toBeHidden();
  await summaries.first().click();
  await expect(detail).toContainText("未进入回答但已产生 Token／费用");
  await expect(detail).toContainText("unknown · 费用未知");
  await expect(detail).not.toContainText("999");
  await expect(detail).toContainText("estimated · USD 0.006");
  await expect(detail).toContainText("output: catalog · stale");
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
  const summary = detail.locator("details > summary");
  await summary.focus();
  await domain(page, 2, session, {name: "block.started", index: 0, block_type: "text"}, {attempt: "a", step: 0});
  await expect(summary).toBeFocused();
  await domain(page, 3, session, {name: "attempt.aborted", blocks: [{type: "text", text: "作废正文"}]}, {attempt: "a", step: 0});
  await expect(summary).toContainText("aborted");
  await expect(detail.locator("details")).not.toHaveAttribute("open", "");
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
