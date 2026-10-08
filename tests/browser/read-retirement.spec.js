import { test, expect } from "@playwright/test";
import { localServer } from "./local-server.js";
import { memoryServer } from "./memory-server.js";

// Only delay delivery of a successful response from the real service. The
// production reader, source identity and returned body are not substituted.
async function holdResponse(page, pattern) {
  let release, fetched, delivered;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const received = new Promise((resolve) => {
    fetched = resolve;
  });
  const delivery = new Promise((resolve) => {
    delivered = resolve;
  });
  await page.route(
    pattern,
    async (route) => {
      const response = await route.fetch();
      fetched();
      await gate;
      await route.fulfill({ response }).catch(() => {});
      delivered();
    },
    { times: 1 },
  );
  return {
    received,
    async release() {
      release();
      await delivery;
    },
  };
}

test("real Session response is retired across disconnect and same-instance reconnect", async ({
  page,
  context,
}) => {
  const server = await localServer();
  let held;
  try {
    await page.goto(server.origin + "/inbox");
    const list = page.getByRole("region", { name: "会话列表" });
    await expect(list.locator("[data-source-id]")).toHaveCount(1);
    const entry = await (
      await page.request.get(server.origin + "/api/entry")
    ).json();
    const created = await page.request.post(server.origin + "/api/sessions", {
      headers: { "x-agent-alfred-csrf": entry.csrf_token },
      data: {},
    });
    expect(created.status()).toBe(201);
    const session = (await created.json()).session_id;
    held = await holdResponse(page, "**/api/sessions?**");
    const refresh = list.getByRole("button", { name: "刷新会话", exact: true });
    await refresh.click();
    await held.received;
    await context.setOffline(true);
    await expect(list).toContainText("连接中断，保留旧快照");
    await context.setOffline(false);
    await expect(page.locator("#shell-status")).not.toContainText("连接中断");
    await held.release();
    await page.evaluate(() => new Promise(requestAnimationFrame));
    await expect(list.locator("[data-source-id]")).toHaveCount(1);
    await expect(list).toContainText("连接中断，保留旧快照");
    await expect(refresh).toBeEnabled();
    await refresh.click();
    await expect(list.locator("[data-source-id]")).toHaveCount(2);
    await expect(
      list.locator('[data-source-id="' + session + '"]'),
    ).toHaveCount(1);
    await expect(list).not.toContainText("连接中断，保留旧快照");
  } finally {
    await context.setOffline(false);
    if (held) await held.release();
    await server.close();
  }
});

for (const source of ["summary", "evidence"])
  test(`active Run ${source} retires its response without clearing stale after reconnect`, async ({
    page,
    context,
  }) => {
    const server = await memoryServer({
      script: "tests/browser/trace_export_server.py",
    });
    let held;
    try {
      await page.goto(server.origin + "/inbox");
      await page.getByRole("button", { name: "新建会话", exact: true }).click();
      await server.send("recording-hold-fail");
      await page
        .getByRole("textbox", { name: "消息", exact: true })
        .fill("运行来源退休验证");
      const accepted = page.waitForResponse(
        (r) =>
          new URL(r.url()).pathname === "/api/runs" &&
          r.request().method() === "POST",
      );
      await page.getByRole("button", { name: "发送", exact: true }).click();
      const { run_id } = await (await accepted).json();
      await server.send("await-recording");
      await page.goto(server.origin + "/runs/" + encodeURIComponent(run_id));
      const statuses = page.locator(".page-body > [role=status]");
      const status = statuses.filter({
        hasText: source === "summary" ? /^运行摘要 ·/ : /^过程证据共同来源 ·/,
      });
      await expect(status).toContainText("读取于");
      const observed = await status.textContent();
      await page.clock.setFixedTime(new Date(Date.now() + 120000));
      const pattern =
        source === "summary"
          ? "**/api/runs/locate/**"
          : "**/api/run-evidence?**";
      held = await holdResponse(page, pattern);
      const refresh = page.getByRole("button", {
        name: source === "summary" ? "刷新运行摘要" : "刷新过程证据",
        exact: true,
      });
      await refresh.click();
      await held.received;
      await context.setOffline(true);
      await expect(status).toContainText("连接中断，保留旧快照");
      await context.setOffline(false);
      await expect(page.locator("#shell-status")).not.toContainText("连接中断");
      await held.release();
      await page.evaluate(() => new Promise(requestAnimationFrame));
      await expect(status).toHaveText(observed + " · 连接中断，保留旧快照");
      await expect(refresh).toBeEnabled();
      await refresh.click();
      await expect(status).not.toContainText("连接中断，保留旧快照");
      await expect(status).not.toHaveText(observed);
    } finally {
      await context.setOffline(false);
      if (held) await held.release();
      await server.send("release-recording");
      await server.close();
    }
  });
