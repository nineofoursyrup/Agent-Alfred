import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { memoryServer } from "./memory-server.js";
import { localServer } from "./local-server.js";

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
  await expect(
    page
      .getByRole("region", { name: "会话只读预览" })
      .locator('[data-source-highlighted="true"]')
      .getByRole("link", { name: "查看运行", exact: true }),
  ).toBeFocused();
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

test("Inbox header creates through the existing owner while retaining list and preview", async ({
  page,
}) => {
  const server = await localServer();
  try {
    await page.goto(server.origin + "/inbox");
    const list = page.getByRole("region", { name: "会话列表" });
    await expect(list.locator("[data-source-id]")).toHaveCount(1);
    const sessionIds = await list
      .locator("[data-source-id]")
      .evaluateAll((rows) => rows.map((row) => row.dataset.sourceId));
    await page.getByRole("button", { name: "收起主对话", exact: true }).click();
    const mutations = [];
    page.on("request", (request) => {
      if (request.method() === "POST")
        mutations.push(new URL(request.url()).pathname);
    });
    const create = async (region, hold = false) => {
      await expect(
        region.getByRole("button", { name: /新建会话/ }),
      ).toBeVisible();
      let release, fetched;
      const gate = new Promise((resolve) => (release = resolve));
      const received = new Promise((resolve) => (fetched = resolve));
      if (hold)
        await page.route(
          "**/api/sessions",
          async (route) => {
            const response = await route.fetch();
            fetched();
            await gate;
            await route.fulfill({ response });
          },
          { times: 1 },
        );
      const reply = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === "/api/sessions" &&
          response.request().method() === "POST",
      );
      await region.getByRole("button", { name: /新建会话/ }).click();
      if (hold) {
        try {
          await received;
          await expect(
            region.getByRole("button", { name: /新建会话/ }),
          ).toBeDisabled();
          await expect(page.locator("#new-session")).toBeDisabled();
          await expect(
            region.getByRole("button", { name: "继续此会话", exact: true }),
          ).toBeDisabled();
          await expect(region).toContainText("正在新建会话，请等待完成。");
        } finally {
          release();
        }
      }
      const response = await reply;
      expect(response.status()).toBe(201);
      const { session_id } = await response.json();
      await expect(page.locator("#mainbar")).toBeVisible();
      await expect(
        page.getByRole("textbox", { name: "消息", exact: true }),
      ).toBeEnabled();
      expect(
        await page.evaluate(() => sessionStorage.getItem("alfred.session")),
      ).toBe(session_id);
      return session_id;
    };
    const first = await create(list);
    await expect(page).toHaveURL(/\/inbox$/);
    expect(
      await list
        .locator("[data-source-id]")
        .evaluateAll((rows) => rows.map((row) => row.dataset.sourceId)),
    ).toEqual(sessionIds);
    await page
      .getByRole("textbox", { name: "消息", exact: true })
      .fill("新建前会话草稿");
    await list
      .getByRole("link", { name: "升级前消息 01", exact: true })
      .click();
    const preview = page.getByRole("region", { name: "会话只读预览" });
    await expect(preview.locator("[data-source-id]")).toHaveCount(25);
    const before = await preview.locator(".inbox-rows").textContent();
    const route = page.url();
    await page.getByRole("button", { name: "收起主对话", exact: true }).click();
    const second = await create(preview, true);
    expect(second).not.toBe(first);
    expect(page.url()).toBe(route);
    expect(await preview.locator(".inbox-rows").textContent()).toBe(before);
    expect(
      await page.evaluate(
        (id) => sessionStorage.getItem("alfred.draft:" + id),
        first,
      ),
    ).toBe("新建前会话草稿");
    expect(mutations).toEqual(["/api/sessions", "/api/sessions"]);
  } finally {
    await server.close();
  }
});

test("Inbox Continue shows the real pending guard without opening the hidden MainBar", async ({
  page,
}) => {
  const server = await memoryServer({
    script: "tests/browser/trace_export_server.py",
  });
  try {
    // Hold native Host state delivery to exercise the authoritative POST race.
    // Every buffered event is later delivered unchanged and in original order.
    await page.addInitScript(() => {
      window.holdHostState = false;
      window.heldHostStates = [];
      window.releaseHostState = () => {
        window.holdHostState = false;
        window.heldHostStates.splice(0).forEach((deliver) => deliver());
      };
      const Native = window.EventSource;
      window.EventSource = class extends Native {
        addEventListener(type, listener, options) {
          if (type === "state_patch")
            super.addEventListener(
              type,
              (event) => {
                const deliver = () => listener.call(this, event);
                if (window.holdHostState) window.heldHostStates.push(deliver);
                else deliver();
              },
              options,
            );
          else super.addEventListener(type, listener, options);
        }
      };
    });
    await page.goto(server.origin + "/inbox");
    await page.locator("#new-session").click();
    await expect(
      page.getByRole("textbox", { name: "消息", exact: true }),
    ).toBeEnabled();
    const a = await page.evaluate(() =>
      sessionStorage.getItem("alfred.session"),
    );
    await page
      .getByRole("textbox", { name: "消息", exact: true })
      .fill("A 保留的草稿");
    const entry = await (
      await page.request.get(server.origin + "/api/entry")
    ).json();
    const headers = { "x-agent-alfred-csrf": entry.csrf_token };
    const b = (
      await (
        await page.request.post(server.origin + "/api/sessions", {
          headers,
          data: {},
        })
      ).json()
    ).session_id;
    await page.getByRole("button", { name: "收起主对话", exact: true }).click();
    await page.goto(
      server.origin +
        "/inbox?" +
        new URLSearchParams({ session_id: b, view: "messages" }),
    );
    const preview = page.getByRole("region", { name: "会话只读预览" });
    await expect(preview).toContainText("此分区暂无已读记录");
    const sessionsBefore = (
      await (await page.request.get(server.origin + "/api/sessions")).json()
    ).sessions.map((item) => item.session_id);
    await page.evaluate(() => (window.holdHostState = true));
    await server.send("recording-hold");
    const accepted = await page.request.post(server.origin + "/api/runs", {
      headers,
      data: { session_id: a, message: "中央继续守卫" },
    });
    expect(accepted.status()).toBe(202);
    await server.send("await-recording");
    const rejected = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/sessions" &&
        response.request().method() === "POST",
    );
    await preview.getByRole("button", { name: /新建会话/ }).click();
    const refusal = await rejected;
    expect(refusal.status()).toBe(409);
    expect((await refusal.json()).code).toBe("mutation_in_flight");
    await expect(preview).toContainText(
      "当前运行或保存操作尚未收尾，请稍后重试。",
    );
    await expect(page.locator("#mainbar")).toBeHidden();
    const sessionsAfter = await (
      await page.request.get(server.origin + "/api/sessions")
    ).json();
    expect(
      [
        ...sessionsAfter.sessions,
        ...(sessionsAfter.non_terminal ? [sessionsAfter.non_terminal] : []),
      ]
        .map((item) => item.session_id)
        .sort(),
    ).toEqual([...sessionsBefore].sort());
    await page.evaluate(() => window.releaseHostState());
    await expect(page.locator("#shell-status")).toContainText("正在保存");
    const resume = preview.getByRole("button", {
      name: "继续此会话",
      exact: true,
    });
    await expect(resume).toBeDisabled();
    await expect(preview).toContainText("当前会话的运行尚未收尾");
    await expect(page.locator("#mainbar")).toBeHidden();
    expect(
      await page.evaluate(() => sessionStorage.getItem("alfred.session")),
    ).toBe(a);
    expect(await page.locator("#message").inputValue()).toBe("A 保留的草稿");
    await server.send("release-recording");
    await expect(resume).toBeEnabled();
    await expect(preview).not.toContainText("当前会话的运行尚未收尾");
    await resume.click();
    expect(
      await page.evaluate(() => sessionStorage.getItem("alfred.session")),
    ).toBe(b);
    await expect(page.locator("#mainbar")).toBeVisible();
    expect(
      await page.evaluate(
        (id) => sessionStorage.getItem("alfred.draft:" + id),
        a,
      ),
    ).toBe("A 保留的草稿");
  } finally {
    await server.send("release-recording");
    await server.close();
  }
});

for (const view of [null, "messages", "runs"])
  test(`Inbox ${view || "sessions"} announces real durable changes without replacing a read window`, async ({
    page,
  }) => {
    const server = await memoryServer({
      script: "tests/browser/overview_server.py",
      prepare: async (directory) => {
        execFileSync(".venv/bin/python", [
          "-B",
          "-c",
          `
import sqlite3,json,sys
from pathlib import Path
from agent_alfred import schema
conn=sqlite3.connect(Path(sys.argv[1])/'db.sqlite3');schema.migrate(conn)
conn.execute('INSERT INTO sessions(session_id,created_at,activity_revision) VALUES (?,?,?)',('source-history','2026-10-01T00:00:00Z',schema.allocate_activity_revision(conn)))
for index in range(30):
 run='source-run-'+str(index)
 conn.execute("INSERT INTO runs(run_id,purpose,session_id,gateway,prompt_preview,phase,outcome,accepted_at,started_at,finished_at,activity_revision,admission_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(run,'chat','source-history','cli','已保存的历史','finished','completed','2026-10-01T00:00:00Z','2026-10-01T00:00:01Z','2026-10-01T00:00:02Z',schema.allocate_activity_revision(conn),'admitted'))
 for role in ['user','assistant']:
  conn.execute('INSERT INTO agent_log(session_id,role,content,source,created_at,run_id) VALUES (?,?,?,?,?,?)',('source-history',role,json.dumps([{'type':'text','text':'历史阅读窗口'}]),'cli','2026-10-01T00:00:02Z',run))
conn.commit();conn.close()
`,
          directory,
        ]);
      },
    });
    try {
      const entry = await (
        await page.request.get(server.origin + "/api/entry")
      ).json();
      const headers = { "x-agent-alfred-csrf": entry.csrf_token };
      const initial = await (
        await page.request.get(server.origin + "/api/sessions")
      ).json();
      const session = initial.sessions[0].session_id;
      const sourcePath =
        view === "messages"
          ? "/api/sessions/messages"
          : view === "runs"
            ? "/api/sessions/runs"
            : "/api/sessions";
      const initialRead = page.waitForResponse(
        (response) => new URL(response.url()).pathname === sourcePath,
      );
      await page.goto(
        server.origin +
          "/inbox" +
          (view
            ? "?" + new URLSearchParams({ session_id: session, view })
            : ""),
      );
      const originalCursor = (await (await initialRead).json()).next_cursor;
      const source = page.getByRole("region", {
        name: view ? "会话只读预览" : "会话列表",
      });
      await expect(source.locator("[data-source-id]").first()).toBeVisible();
      const rows = await source
        .locator("[data-source-id]")
        .evaluateAll((items) => items.map((item) => item.dataset.sourceId));
      const status = source
        .locator("[role=status]")
        .filter({ hasText: /读取于/ });
      const observed = await status.textContent();
      const focus = source.locator(".inbox-rows a").first();
      await focus.focus();
      let sourceReads = 0;
      page.on("request", (request) => {
        if (
          new URL(request.url()).pathname ===
          (view === "messages"
            ? "/api/sessions/messages"
            : view === "runs"
              ? "/api/sessions/runs"
              : "/api/sessions")
        )
          sourceReads++;
      });
      await server.send("hold-recording");
      const accepted = await page.request.post(server.origin + "/api/runs", {
        headers,
        data: { session_id: session, message: "显式刷新才纳入新记录" },
      });
      expect(accepted.status()).toBe(202);
      const { run_id } = await accepted.json();
      await server.send("wait-recording");
      await server.send("release-recording");
      await expect
        .poll(() =>
          page.evaluate(async () => {
            const { dashboard } = await import("/assets/app.js");
            return dashboard.runtime().active;
          }),
        )
        .toBeNull();
      await expect(status).toContainText(/新数据|新运行/);
      await expect(status).toContainText(observed);
      expect(
        await source
          .locator("[data-source-id]")
          .evaluateAll((items) => items.map((item) => item.dataset.sourceId)),
      ).toEqual(rows);
      await expect(focus).toBeFocused();
      expect(sourceReads).toBe(0);
      if (view) {
        expect(typeof originalCursor).toBe("string");
        const nextRequest = page.waitForRequest(
          (request) => new URL(request.url()).pathname === sourcePath,
        );
        await source
          .getByRole("button", {
            name: view === "messages" ? "更多会话消息" : "更多会话运行",
            exact: true,
          })
          .click();
        expect(
          new URL((await nextRequest).url()).searchParams.get("cursor"),
        ).toBe(originalCursor);
        await expect(source.locator("[data-source-id]")).toHaveCount(
          view === "messages" ? 62 : 30,
        );
        expect(
          await source
            .locator("[data-source-id]")
            .evaluateAll(
              (items, count) =>
                items.slice(0, count).map((item) => item.dataset.sourceId),
              rows.length,
            ),
        ).toEqual(rows);
        await expect(status).toContainText(/新数据|新运行/);
      }
      await source
        .getByRole("button", {
          name:
            view === "messages"
              ? "刷新会话消息"
              : view === "runs"
                ? "刷新会话运行"
                : "刷新会话",
          exact: true,
        })
        .click();
      await expect(status).not.toContainText(/新数据|新运行/);
      expect(sourceReads).toBe(view ? 2 : 1);
      if (view === "messages") {
        // Messages retain their chronological Run segment. Refresh starts its
        // first page; the new last Run is reached only by explicit pagination.
        for (const count of [62]) {
          await source
            .getByRole("button", { name: "更多会话消息", exact: true })
            .click();
          await expect(source.locator("[data-source-id]")).toHaveCount(count);
        }
      }
      if (view)
        await expect(
          source.locator(
            'a[href="/runs/' + encodeURIComponent(run_id) + '?filter=chat"]',
          ),
        ).toHaveCount(view === "messages" ? 2 : 1);
    } finally {
      await server.send("release-recording");
      await server.close();
    }
  });
