import { test, expect } from "@playwright/test";

test("Database page lists approved objects without executing examples", async ({
  page,
}) => {
  await page.goto("/database");
  await expect(page.getByRole("heading", { name: "数据库", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "MainBar" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "SQL" })).toBeVisible();
  const catalogDetails = page.locator('.db-catalog > details').first();
  await expect(catalogDetails).not.toHaveAttribute('open');
  await page.getByText('对象目录',{exact:true}).click();
  await expect(page.getByText("diag_sessions", { exact: true })).toBeVisible();
  await expect(page.getByText("diag_memory_mirrors", { exact: true })).toBeVisible();
  await expect(page.getByText("当前实例固定")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("db.sqlite3");
  await expect(page.locator("body")).not.toContainText("CREATE TABLE sessions");
  await expect(page.getByRole("region", { name: "查询结果" })).toBeEmpty();
});

test("chat then Database JOIN matches the saved assistant reply", async ({
  page,
}) => {
  await page.goto("/inbox");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  const input = page.getByRole("textbox", { name: "消息" });
  await input.fill("请回复");
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
  const accepted = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/runs") &&
      response.request().method() === "POST",
  );
  await input.press("Enter");
  expect((await accepted).status()).toBe(202);
  const conversation = page.getByRole("region", { name: "主对话" });
  await expect(
    conversation.getByText("离线模型回复", { exact: true }),
  ).toHaveCount(1);
  await expect(conversation.getByText("已保存", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "数据库", exact: true }).click();
  await expect(page.getByRole("heading", { name: "数据库", exact: true })).toBeVisible();
  const editor = page.getByRole("textbox", { name: "SQL" });
  await editor.fill(
    "SELECT m.text FROM diag_messages m WHERE m.role='assistant'",
  );
  const executed = page.waitForResponse(
    (response) =>
      response.url().includes("/execute") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "执行", exact: true }).click();
  expect((await executed).status()).toBe(200);
  await expect(page.getByRole("region", { name: "查询结果" })).toContainText(
    "离线模型回复",
  );
});

test("empty SELECT shows columns and zero rows; syntax error stays safe", async ({
  page,
}) => {
  await page.goto("/database");
  const editor = page.getByRole("textbox", { name: "SQL" });
  await expect(page.getByText("可执行")).toBeVisible();
  await editor.fill("SELECT 1 AS n WHERE 1=0");
  const executed = page.waitForResponse(
    (response) =>
      response.url().includes("/execute") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "执行", exact: true }).click();
  expect((await executed).status()).toBe(200);
  await expect(page.getByText("0 行", {exact:true})).toBeVisible();
  await expect(page.locator("th", { hasText: "n" })).toBeVisible();
  await editor.fill("SELECT FROM");
  const failed = page.waitForResponse(
    (response) =>
      response.url().includes("/execute") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "执行", exact: true }).click();
  const response = await failed;
  expect(response.status()).toBe(400);
  const body = await response.json();
  expect(body.code).toBe("sql_error");
  expect(JSON.stringify(body)).not.toContain("SELECT FROM");
  await expect(page.getByText("执行错误")).toBeVisible();
});

test("leaving Database cancels and clears draft and results", async ({ page }) => {
  await page.goto("/database");
  await expect(page.getByText("可执行")).toBeVisible();
  const editor = page.getByRole("textbox", { name: "SQL" });
  await editor.fill("SELECT 1 AS n");
  const executed = page.waitForResponse(
    (response) =>
      response.url().includes("/execute") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "执行", exact: true }).click();
  expect((await executed).status()).toBe(200);
  await expect(page.getByRole("region", { name: "查询结果" })).toContainText("1");
  await page.getByRole("link", { name: "收件箱", exact: true }).click();
  await page.getByRole("button", { name: "放弃并离开", exact: true }).click();
  await page.getByRole("link", { name: "数据库", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "SQL" })).toHaveValue("");
  await expect(page.getByRole("region", { name: "查询结果" })).toBeEmpty();
});

test("HTML in a result cell is not executed", async ({ page }) => {
  await page.goto("/database");
  await expect(page.getByText("可执行")).toBeVisible();
  const editor = page.getByRole("textbox", { name: "SQL" });
  await editor.fill("SELECT '<img src=x onerror=alert(1)>' AS html");
  const executed = page.waitForResponse(
    (response) =>
      response.url().includes("/execute") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "执行", exact: true }).click();
  expect((await executed).status()).toBe(200);
  const result = page.getByRole("region", { name: "查询结果" });
  await expect(result).toContainText("<img src=x onerror=alert(1)>");
  await expect(result.locator("img")).toHaveCount(0);
});

test("successful result shows read time, sources, protection and coverage", async ({
  page,
}) => {
  await page.goto("/database");
  await expect(page.getByText("可执行")).toBeVisible();
  const editor = page.getByRole("textbox", { name: "SQL" });
  await editor.fill("SELECT count(*) FROM diag_attempts");
  const executed = page.waitForResponse(
    (response) =>
      response.url().includes("/execute") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "执行", exact: true }).click();
  expect((await executed).status()).toBe(200);
  const result = page.getByRole("region", { name: "查询结果" });
  await expect(result).toContainText("读取");
  await expect(result).toContainText("来源");
  await expect(result).toContainText("保护");
  await expect(result).toContainText("缺行不证明未发生请求");
  await expect(result).toContainText("unrecorded");
});

test("hidden tab keeps results; offline clears results and keeps SQL", async ({
  page,
}) => {
  await page.goto("/database");
  await expect(page.getByText("可执行")).toBeVisible();
  const editor = page.getByRole("textbox", { name: "SQL" });
  await editor.fill("SELECT 1 AS n");
  const executed = page.waitForResponse(
    (response) =>
      response.url().includes("/execute") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "执行", exact: true }).click();
  expect((await executed).status()).toBe(200);
  const result = page.getByRole("region", { name: "查询结果" });
  await expect(result).toContainText("1");
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(result).toContainText("1");
  await expect(editor).toHaveValue("SELECT 1 AS n");
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await expect(result).toBeEmpty();
  await expect(editor).toHaveValue("SELECT 1 AS n");
  await expect(page.locator("#page [data-state]")).toHaveText("不可用 · 连接中断");
});

test("BFCache restore clears results and SQL then rechecks", async ({ page }) => {
  await page.goto("/database");
  await expect(page.getByText("可执行")).toBeVisible();
  const editor = page.getByRole("textbox", { name: "SQL" });
  await editor.fill("SELECT 1 AS n");
  const executed = page.waitForResponse(
    (response) =>
      response.url().includes("/execute") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "执行", exact: true }).click();
  expect((await executed).status()).toBe(200);
  await expect(page.getByRole("region", { name: "查询结果" })).toContainText("1");
  await page.evaluate(() => {
    const event = new Event("pageshow");
    Object.defineProperty(event, "persisted", { get: () => true });
    window.dispatchEvent(event);
  });
  await expect(page.getByRole("textbox", { name: "SQL" })).toHaveValue("");
  await expect(page.getByRole("region", { name: "查询结果" })).toBeEmpty();
  await expect(page.getByText("可执行")).toBeVisible();
});

test("one click cannot start two execute requests", async ({ page }) => {
  await page.goto("/database");
  await expect(page.getByText("可执行")).toBeVisible();
  const editor = page.getByRole("textbox", { name: "SQL" });
  await editor.fill("SELECT 1 AS n");
  /** @type {string[]} */
  const executes = [];
  page.on("request", (request) => {
    if (request.url().includes("/execute") && request.method() === "POST") {
      executes.push(request.url());
    }
  });
  await page.getByRole("button", { name: "执行", exact: true }).evaluate((button) => {
    button.click();
    button.click();
  });
  await expect(page.getByRole("region", { name: "查询结果" })).toContainText("1");
  expect(executes.length).toBe(1);
});

test("real SSE recovery clears results, verifies versions and never reruns SQL", async ({page}) => {
  const {api, memoryServer} = await import("./memory-server.js");
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({operation_id: "db-seed", kind: "semantic", action: "save",
      payload: {subject: "database", fact: "forget-this-real-diagnostic-body"}});
    let streams = 0, catalogs = 0, executions = 0;
    page.on("request", request => {
      if (request.url().includes("/api/events")) streams++;
      if (request.url().endsWith("/api/database")) catalogs++;
      if (request.url().endsWith("/execute")) executions++;
    });
    await page.goto(server.origin + "/database");
    const editor = page.getByRole("textbox", {name: "SQL"});
    const result = page.getByRole("region", {name: "查询结果"});
    await expect(page.getByText("可执行", {exact: true})).toBeVisible();
    await editor.fill("SELECT fact FROM diag_facts");
    await page.getByRole("button", {name: "执行", exact: true}).click();
    await expect(result).toContainText("forget-this-real-diagnostic-body");
    const before = {streams, catalogs, executions};
    await server.send("overflow");
    const deleted = await other.command({operation_id: "db-delete", kind: "semantic", action: "delete",
      expected_version: 1, payload: {id: saved.body.result.memory_id}});
    expect(deleted.body.result.status).toBe("deleted");
    await expect(result).toBeEmpty();
    await expect.poll(() => streams).toBeGreaterThan(before.streams);
    await expect.poll(() => catalogs).toBeGreaterThan(before.catalogs);
    await expect(page.getByText("可执行", {exact: true})).toBeVisible();
    await expect(editor).toHaveValue("SELECT fact FROM diag_facts");
    expect(executions).toBe(before.executions);
    await page.getByRole("button", {name: "执行", exact: true}).click();
    await expect(result).toContainText("0 行");
    await expect(result).not.toContainText("forget-this-real-diagnostic-body");
  } finally {
    await server.close();
  }
});

test("real deletion rejects an old HTTP response delayed at the browser boundary", async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  const {api, memoryServer} = await import("./memory-server.js");
  const server = await memoryServer();
  let resume;
  const gate = new Promise(resolve => { resume = resolve; });
  let arrived;
  const ready = new Promise(resolve => { arrived = resolve; });
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({operation_id: "late-seed", kind: "semantic", action: "save",
      payload: {subject: "database", fact: "late-forgotten-diagnostic-body"}});
    await page.goto(server.origin + "/database");
    const editor = page.getByRole("textbox", {name: "SQL"});
    const result = page.getByRole("region", {name: "查询结果"});
    await expect(page.getByText("可执行", {exact: true})).toBeVisible();
    await page.route("**/api/database/queries/*/execute", async route => {
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      arrived();
      await gate;
      await route.fulfill({response});
    });
    await editor.fill("SELECT fact FROM diag_facts");
    await page.getByRole("button", {name: "执行", exact: true}).click();
    await ready;
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    const deleted = await other.command({operation_id: "late-delete", kind: "semantic", action: "delete",
      expected_version: 1, payload: {id: saved.body.result.memory_id}});
    expect(deleted.body.result.status).toBe("deleted");
    expect(deleted.body.forgetting.state).toBe("complete");
    // Cleanup publishes intermediate revisions while writes can make reads
    // unavailable. Verify the settled revision and the page's own recheck.
    const verified = await other.get("/api/database");
    expect(verified.status).toBe(200);
    expect(verified.body).toMatchObject({available: true,
      instance_id: deleted.body.process_instance_id,
      memory_revision: String(deleted.body.memory_revision)});
    await expect(page.getByRole("region", {name: "诊断对象目录",includeHidden:true})).toContainText(
      `记忆修订 ${verified.body.memory_revision} · 保护规则版本 ${verified.body.protection_version}`);
    await expect(page.getByRole("region", {name:"已提交查询",includeHidden:true})).toBeEmpty();
    await expect(page.getByRole("button", {name:"复制本页结果",exact:true,includeHidden:true})).toBeDisabled();
    await page.getByRole("button", {name:"收起主对话",exact:true}).click();
    await expect(page.getByText("可执行", {exact: true})).toBeVisible();
    resume();
    await page.unrouteAll({behavior: "wait"});
    await expect(result).toBeEmpty();
    await expect(editor).toHaveValue("SELECT fact FROM diag_facts");
    await page.getByRole("button", {name: "执行", exact: true}).click();
    await expect(result).toContainText("0 行");
    await expect(result).not.toContainText("late-forgotten-diagnostic-body");
  } finally {
    resume();
    await server.close();
  }
});


test("S10 example replacement keeps an existing draft and its selection by default", async ({page}) => {
  await page.goto("/database");
  await expect(page.getByText("可执行", {exact:true})).toBeVisible();
  const editor = page.getByRole("textbox", {name:"SQL", exact:true});
  await editor.fill("SELECT 42 AS retained");
  await editor.evaluate(el => el.setSelectionRange(7, 9));
  let executes = 0;
  page.on("request", r => {if(r.url().endsWith("/execute")) executes++;});
  // The catalog is collapsed in the migrated page, expanded in the baseline.
  const summary = page.getByText("对象目录", {exact:true});
  if (await summary.count()) {
    await summary.click();
    await page.locator('summary').filter({hasText:/^diag_sessions$/}).click();
  }
  const example = page.getByRole("button", {name:"写入编辑器",exact:true}).first();
  await example.click();
  const dialog = page.getByRole("dialog", {name:"替换 SQL 草稿"});
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", {name:"保留当前编辑"})).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(editor).toHaveValue("SELECT 42 AS retained");
  expect(await editor.evaluate(el => [el.selectionStart,el.selectionEnd])).toEqual([7,9]);
  await example.click();
  await dialog.getByRole("button", {name:"放弃后替换"}).click();
  await expect(editor).toHaveValue(/FROM diag_sessions LIMIT 20/);
  await editor.press("Enter");
  expect(executes).toBe(0);
});

test("S10 submitted SQL keeps its identity while the next draft changes", async ({page}) => {
  await page.goto("/database");
  await expect(page.getByText("可执行", {exact:true})).toBeVisible();
  let ready, release;
  const arrived = new Promise(resolve => {ready = resolve;});
  const gate = new Promise(resolve => {release = resolve;});
  const sql = "SELECT 42 AS query_A";
  await page.route("**/api/database/queries/*/execute", async route => {
    expect(route.request().postDataJSON().sql).toBe(sql);
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    ready();
    await gate;
    await route.fulfill({response});
  });
  try {
    const editor = page.getByRole("textbox", {name:"SQL",exact:true});
    await editor.fill(sql);
    await page.getByRole("button", {name:"执行",exact:true}).click();
    await arrived;
    await editor.fill("SELECT 7 AS next_draft");
    release();
    const submitted = page.getByRole("region", {name:"已提交查询"});
    await expect(submitted).toContainText(sql);
    await expect(submitted).not.toContainText("next_draft");
    await expect(page.getByText("当前草稿已改变；结果仍属于本次提交。",{exact:true})).toBeVisible();
    await expect(page.getByRole("region", {name:"查询结果"}).locator("th")).toHaveText("query_A");
    await page.evaluate(() => window.dispatchEvent(new Event("offline")));
    await expect(submitted).toBeEmpty();
    await expect(editor).toHaveValue("SELECT 7 AS next_draft");
  } finally {release();}
});

test('S10 copies only the rendered result page with row-limit and source evidence', async ({page,context}) => {
  await context.grantPermissions(['clipboard-read','clipboard-write']);
  await page.goto('/database');
  await expect(page.getByText('可执行',{exact:true})).toBeVisible();
  const values = Array.from({length:11}, (_,i) => `SELECT ${i} AS n`).join(' UNION ALL ');
  const sql = `WITH digits AS (${values}) SELECT a.n*121+b.n*11+c.n AS n FROM digits a CROSS JOIN digits b CROSS JOIN digits c ORDER BY n`;
  const editor = page.getByRole('textbox',{name:'SQL',exact:true});
  await editor.fill(sql);
  let executions = 0;
  page.on('request', r => {if(r.url().endsWith('/execute')) executions++;});
  await page.getByRole('button',{name:'执行',exact:true}).click();
  const result = page.getByRole('region',{name:'查询结果'});
  await expect(result.locator('tbody tr')).toHaveCount(100);
  await page.getByRole('button',{name:'下一页',exact:true}).click();
  await expect(result.locator('td').first()).toHaveText('100');
  await expect(result.locator('td').last()).toHaveText('199');
  await page.getByRole('button',{name:'复制本页结果',exact:true}).click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain('第 101–200 行');
  expect(copied).toContain('本次返回 1000 行');
  expect(copied).toContain('截断');
  expect(copied).toContain('行数上限');
  expect(copied).toContain('读取');
  expect(copied).toContain('来源');
  expect(copied).toContain('覆盖');
  expect(copied).toContain('\n100\n');
  expect(copied).not.toContain('\n0\n');
  expect(copied).not.toContain('\n200\n');
  expect(executions).toBe(1);
  await editor.fill('SELECT 9 AS draft_only');
  await page.getByRole('button',{name:'复制 SQL',exact:true}).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('SELECT 9 AS draft_only');
});
