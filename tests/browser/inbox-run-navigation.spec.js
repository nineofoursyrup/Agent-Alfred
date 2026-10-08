import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { memoryServer } from "./memory-server.js";

test("real Inbox preview, Run source return and explicit MainBar location preserve the selected Session draft", async ({
  page,
}) => {
  const reads = [],
    writes = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST") writes.push(path);
    if (path.endsWith("/locate") || path.startsWith("/api/runs/locate/"))
      reads.push(request.url());
  });
  await page.goto("/inbox");
  async function create() {
    const received = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === "/api/sessions" &&
        r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "新建会话", exact: true }).click();
    const body = await (await received).json();
    await expect(
      page.getByRole("textbox", { name: "消息", exact: true }),
    ).toBeEnabled();
    return body.session_id;
  }
  const b = await create();
  await page
    .getByRole("textbox", { name: "消息", exact: true })
    .fill("来源返回的真实会话 B");
  const accepted = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/api/runs" &&
      r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const { run_id } = await (await accepted).json();
  await expect(
    page.locator(`#messages [data-run-id="${run_id}"]`),
  ).toContainText("已保存");
  const a = await create();
  await page
    .getByRole("textbox", { name: "消息", exact: true })
    .fill("A 的未提交草稿");
  await page.getByRole("button", { name: "收起主对话", exact: true }).click();
  const before = writes.length;
  await page.goto(
    "/inbox?" + new URLSearchParams({ session_id: b, view: "messages" }),
  );
  await expect(
    page.getByRole("region", { name: "会话只读预览" }),
  ).toContainText("来源返回的真实会话 B");
  expect(
    await page.evaluate(() => sessionStorage.getItem("alfred.session")),
  ).toBe(a);
  await expect(
    page
      .getByRole("region", { name: "会话只读预览" })
      .getByRole("link", { name: "查看运行", exact: true }),
  ).toHaveCount(2);
  await page
    .getByRole("region", { name: "会话只读预览" })
    .getByRole("link", { name: "查看运行", exact: true })
    .last()
    .click();
  await expect(page.getByRole("region", { name: "运行摘要" })).toContainText(
    run_id,
  );
  await expect(page.getByRole("region", { name: "运行列表" })).toHaveCount(0);
  await page.getByRole("link", { name: "返回来源", exact: true }).click();
  await expect(
    page
      .getByRole("region", { name: "会话只读预览" })
      .locator('[data-source-highlighted="true"]'),
  ).toHaveCount(1);
  expect(
    reads.filter(
      (url) => new URL(url).pathname === "/api/sessions/messages/locate",
    ),
  ).toHaveLength(1);
  await page
    .getByRole("region", { name: "会话只读预览" })
    .getByRole("link", { name: "查看运行", exact: true })
    .last()
    .click();
  await page
    .getByRole("button", { name: "切换到此会话并在主对话中查看", exact: true })
    .click();
  await expect(
    page.locator(`#messages [data-run-id="${run_id}"]`),
  ).toContainText("离线模型回复");
  expect(
    reads.filter((url) => new URL(url).pathname === "/api/mainbar/locate"),
  ).toHaveLength(1);
  expect(writes.length).toBe(before);
  await page.evaluate(async (session) => {
    const { dashboard } = await import("/assets/app.js");
    await dashboard.selectSession(session);
  }, a);
  await expect(
    page.getByRole("textbox", { name: "消息", exact: true }),
  ).toHaveValue("A 的未提交草稿");
});

test("failed source return never scans or substitutes a target, and fallback is explicit", async ({
  page,
}) => {
  const session = "legacy /会话?";
  const reads = [];
  await page.goto(
    "/inbox?" + new URLSearchParams({ session_id: session, view: "messages" }),
  );
  const preview = page.getByRole("region", { name: "会话只读预览" });
  await expect(preview).toContainText("升级前消息 01");
  // A browser Back source stores identifiers only; create it through the
  // public shell port to exercise an aged source whose row was removed.
  const instance = await page.evaluate(async () => {
    const { dashboard } = await import("/assets/app.js");
    return dashboard.runtime().instance;
  });
  await page.route("**/api/sessions/messages/locate?*", (route) => {
    reads.push(route.request().url());
    return route.fulfill({
      status: 404,
      json: { code: "source_target_unavailable" },
    });
  });
  await page
    .getByRole("navigation", { name: "主导航" })
    .getByRole("link", { name: "运行", exact: true })
    .click();
  await page.evaluate(
    async (source) => {
      const { dashboard } = await import("/assets/app.js");
      await dashboard.navigate(source.route, { source });
    },
    {
      route:
        "/inbox?" +
        new URLSearchParams({ session_id: session, view: "messages" }),
      kind: "messages",
      session_id: session,
      anchor: "removed-anchor",
      process_instance_id: instance,
    },
  );
  await expect(preview).toContainText("source_target_unavailable");
  await expect(preview.locator("[data-source-id]")).toHaveCount(0);
  expect(reads).toHaveLength(1);
  await preview
    .getByRole("button", { name: "刷新会话消息", exact: true })
    .click();
  await expect.poll(() => reads.length).toBe(2);
  await expect(preview.locator("[data-source-id]")).toHaveCount(0);
  await preview
    .getByRole("button", { name: "查看最新会话消息列表", exact: true })
    .click();
  await expect(preview.locator("[data-source-id]")).toHaveCount(25);
});

test("message paging failure retains the real opaque cursor and successful source", async ({
  page,
}) => {
  const cursors = [];
  let fail = true;
  await page.route("**/api/sessions/messages?*", async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    cursors.push(cursor);
    if (cursor && fail) {
      fail = false;
      return route.fulfill({ status: 503, json: { code: "read_unavailable" } });
    }
    return route.continue();
  });
  await page.goto(
    "/inbox?" +
      new URLSearchParams({ session_id: "legacy /会话?", view: "messages" }),
  );
  const preview = page.getByRole("region", { name: "会话只读预览" });
  await expect(preview.locator("[data-source-id]")).toHaveCount(25);
  const anchors = await preview
    .locator("[data-source-id]")
    .evaluateAll((rows) =>
      rows.map((row) => row.getAttribute("data-source-id")),
    );
  await preview
    .getByRole("button", { name: "更多会话消息", exact: true })
    .click();
  await expect(preview).toContainText("已读内容保留");
  expect(
    await preview
      .locator("[data-source-id]")
      .evaluateAll((rows) =>
        rows.map((row) => row.getAttribute("data-source-id")),
      ),
  ).toEqual(anchors);
  await preview
    .getByRole("button", { name: "读取暂不可用，点击重试", exact: true })
    .click();
  await expect(preview.locator("[data-source-id]")).toHaveCount(50);
  expect(cursors).toHaveLength(3);
  expect(cursors[1]).toBe(cursors[2]);
  expect(cursors[1]).not.toBeNull();
});

test("ordinary evidence refresh keeps expanded content and a user text selection", async ({
  page,
}) => {
  await page.goto("/inbox");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await page.getByRole("textbox", { name: "消息" }).fill("保留过程阅读位置");
  const accepted = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/api/runs" &&
      r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const { run_id } = await (await accepted).json();
  await expect(
    page.locator(`#messages [data-run-id="${run_id}"]`),
  ).toContainText("已保存");
  await page.goto("/runs/" + encodeURIComponent(run_id));
  const detail = page.getByRole("region", { name: "运行过程" }),
    attempt = detail.locator("details.attempt").first();
  await attempt.locator("summary").click();
  await attempt.getByRole("button", { name: "展开全文", exact: true }).click();
  const text = attempt.locator(".evidence-text");
  await expect(text).toHaveText("离线模型回复");
  let release, entered;
  const gate = new Promise((r) => (release = r)),
    received = new Promise((r) => (entered = r));
  await page.route("**/api/run-evidence?*", async (route) => {
    const response = await route.fetch();
    entered();
    await gate;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "刷新过程证据", exact: true }).click();
  await received;
  await text.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  });
  release();
  await expect(
    page.getByRole("button", { name: "刷新过程证据", exact: true }),
  ).toBeEnabled();
  expect(await page.evaluate(() => getSelection().toString())).toBe(
    "离线模型回复",
  );
  await expect(attempt).toHaveAttribute("open", "");
  await expect(
    attempt.getByRole("button", { name: "收起全文", exact: true }),
  ).toHaveAttribute("aria-expanded", "true");
  await expect(attempt.getByText("离线模型回复", { exact: true })).toHaveCount(
    1,
  );
});

test("late source positioning cannot take focus from a newer MainBar reading intent", async ({
  page,
}) => {
  const session = "legacy /会话?";
  await page.goto(
    "/inbox?" + new URLSearchParams({ session_id: session, view: "messages" }),
  );
  const preview = page.getByRole("region", { name: "会话只读预览" });
  await expect(preview.locator("[data-source-id]")).toHaveCount(25);
  const anchor = await preview
    .locator("[data-source-id]")
    .first()
    .getAttribute("data-source-id");
  await page.getByRole("button", { name: "继续此会话", exact: true }).click();
  const instance = await page.evaluate(async () => {
    const { dashboard } = await import("/assets/app.js");
    return dashboard.runtime().instance;
  });
  await page
    .getByRole("navigation", { name: "主导航" })
    .getByRole("link", { name: "运行", exact: true })
    .click();
  let release, entered;
  const gate = new Promise((r) => (release = r)),
    received = new Promise((r) => (entered = r));
  await page.route("**/api/sessions/messages/locate?*", async (route) => {
    const response = await route.fetch();
    entered();
    await gate;
    await route.fulfill({ response });
  });
  await page.evaluate(
    async (source) => {
      const { dashboard } = await import("/assets/app.js");
      await dashboard.navigate(source.route, { source });
    },
    {
      route:
        "/inbox?" +
        new URLSearchParams({ session_id: session, view: "messages" }),
      kind: "messages",
      session_id: session,
      anchor,
      process_instance_id: instance,
    },
  );
  await received;
  await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  await page.getByRole("textbox", { name: "消息" }).fill("新的阅读意图");
  release();
  await expect(preview.locator("[data-source-id]")).toHaveCount(25);
  await expect(page.getByRole("textbox", { name: "消息" })).toBeFocused();
  await expect(page.getByRole("textbox", { name: "消息" })).toHaveValue(
    "新的阅读意图",
  );
});

for (const failed of [false, true])
  test(`real ${failed ? "failed" : "recorded"} settlement unlocks the original waiting cursor without a refresh`, async ({
    page,
  }) => {
    const server = await memoryServer({
      script: "tests/browser/trace_export_server.py",
      prepare: async (directory) => {
        execFileSync(".venv/bin/python", [
          "-B",
          "-c",
          `
import sqlite3,json,sys
from pathlib import Path
from agent_alfred import schema
conn=sqlite3.connect(Path(sys.argv[1])/'db.sqlite3');schema.migrate(conn)
conn.execute('INSERT INTO sessions(session_id,created_at,activity_revision) VALUES (?,?,?)',('waiting-history','old',schema.allocate_activity_revision(conn)))
for index in range(3):
 conn.execute('INSERT INTO agent_log(session_id,role,content,source,created_at) VALUES (?,?,?,?,?)',('waiting-history','user',json.dumps([{'type':'text','text':'历史等文'}]),'cli','old'))
conn.commit();conn.close()
`,
          directory,
        ]);
      },
    });
    try {
      await page.goto(
        server.origin + "/inbox?session_id=waiting-history&view=messages",
      );
      const preview = page.getByRole("region", { name: "会话只读预览" });
      await expect(preview.locator("[data-source-id]")).toHaveCount(3);
      await page
        .getByRole("button", { name: "继续此会话", exact: true })
        .click();
      await server.send(failed ? "recording-hold-fail" : "recording-hold");
      await page.getByRole("textbox", { name: "消息" }).fill("等待记录边界");
      await page.getByRole("button", { name: "发送", exact: true }).click();
      await server.send("await-recording");
      const pendingResponse = page.waitForResponse(
        (r) => new URL(r.url()).pathname === "/api/sessions/messages",
      );
      await preview
        .getByRole("button", { name: "刷新会话消息", exact: true })
        .click();
      const pending = await (await pendingResponse).json();
      expect(pending.runs_pending).toBe(true);
      expect(typeof pending.next_cursor).toBe("string");
      await expect(preview).toContainText("运行记录尚未落定");
      await expect(preview.locator("[data-source-id]")).toHaveCount(0);
      await expect(
        preview.getByRole("button", { name: "更多会话消息", exact: true }),
      ).toBeDisabled();
      const continuedReads = [];
      page.on("request", (request) => {
        if (new URL(request.url()).pathname === "/api/sessions/messages")
          continuedReads.push(request.url());
      });
      await server.send("release-recording");
      await expect(page.getByRole("region", { name: "主对话" })).toContainText(
        failed ? "未保存" : "已保存",
      );
      const more = preview.getByRole("button", {
        name: "更多会话消息",
        exact: true,
      });
      await expect(more).toBeEnabled();
      await expect(preview).toContainText("可继续读取");
      await expect(preview.locator("[data-source-id]")).toHaveCount(0);
      expect(continuedReads).toHaveLength(0);
      await preview
        .getByRole("button", { name: "更多会话消息", exact: true })
        .click();
      await expect(preview.locator("[data-source-id]")).toHaveCount(
        failed ? 3 : 5,
      );
      expect(continuedReads).toHaveLength(1);
      expect(new URL(continuedReads[0]).searchParams.get("cursor")).toBe(
        pending.next_cursor,
      );
      await expect(
        preview.getByRole("link", { name: "查看运行", exact: true }),
      ).toHaveCount(failed ? 0 : 2);
    } finally {
      await server.send("release-recording");
      await server.close();
    }
  });
