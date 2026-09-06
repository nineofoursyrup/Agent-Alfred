import { test, expect } from "@playwright/test";
import { controlledTransport, domain, emit, state, run } from "./transport.js";

for (const shared of [false, true])
  test(`two tabs ${shared ? "sharing" : "isolating"} a Session project only matching temporary text`, async ({
    page,
    context,
  }) => {
    const first = await controlledTransport(page);
    const other = await context.newPage();
    let second = await controlledTransport(other);
    if (shared) {
      await other.route("**/api/sessions?*", (route) =>
        route.fulfill({
          json: {
            sessions: [
              { session_id: first, title: "共享会话", created_at: "今天" },
            ],
            non_terminal: null,
            next_cursor: null,
          },
        }),
      );
      await other.getByRole("link", { name: "收件箱", exact: true }).click();
      await other
        .getByRole("button", { name: "共享会话", exact: true })
        .click();
      await other
        .getByRole("button", { name: "继续此会话", exact: true })
        .click();
      await expect
        .poll(() => other.evaluate(() => window.sources.length))
        .toBe(3);
      second = first;
    }
    for (const [tab, session] of [
      [page, first],
      [other, second],
    ]) {
      await emit(
        tab,
        "state_patch",
        state(session, 1, {
          coordinator_state: "running",
          active_run: run(first),
        }),
      );
      const expand = tab.getByRole("button", { name: "展开对话", exact: true });
      if (await expand.isVisible()) await expand.click();
      await tab.getByRole("textbox", { name: "消息" }).fill("本地草稿");
      await domain(
        tab,
        1,
        first,
        { name: "attempt.started", attempt_id: "a1" },
        { attempt: "a1", step: 0 },
      );
      await domain(
        tab,
        2,
        first,
        { name: "block.started", index: 0, block_type: "text" },
        { attempt: "a1", step: 0 },
      );
      await domain(
        tab,
        3,
        first,
        { name: "block.delta", index: 0, text: "同一运行的临时文字" },
        { attempt: "a1", step: 0 },
      );
    }
    await expect(page.getByRole("region", { name: "主对话" })).toContainText(
      "同一运行的临时文字",
    );
    const otherChat = other.getByRole("region", { name: "主对话" });
    if (shared) await expect(otherChat).toContainText("同一运行的临时文字");
    else await expect(otherChat).not.toContainText("同一运行的临时文字");
    await expect(other.getByRole("region", { name: "当前运行" })).toContainText(
      "运行中",
    );
    await expect(other.getByRole("textbox", { name: "消息" })).toHaveValue(
      "本地草稿",
    );
    const count = await other.evaluate(() => window.sources.length);
    await other.getByRole("link", { name: "运行", exact: true }).click();
    await expect(
      other.getByRole("heading", { name: "运行详情", exact: true }),
    ).toBeVisible();
    expect(await other.evaluate(() => window.sources.length)).toBe(count);
  });

test("a restarted process invalidates the Session without losing its draft and refreshes the write token", async ({
  page,
}) => {
  const session = await controlledTransport(page);
  await emit(page, "state_patch", state(session, 1));
  await page.getByRole("textbox", { name: "消息" }).fill("重启也保留");
  await page.route("**/api/entry", (route) =>
    route.fulfill({
      json: { instance_id: "new-process", csrf_token: "new-token" },
    }),
  );
  await page.route("**/api/sessions", (route) =>
    route.fulfill({
      status:
        route.request().headers()["x-agent-alfred-csrf"] === "new-token"
          ? 201
          : 403,
      json: { session_id: "new-session" },
    }),
  );
  await emit(page, "error", {});
  await emit(page, "open", {});
  await emit(
    page,
    "state_patch",
    state(session, 0, {
      process_instance_id: "new-process",
      session_valid: false,
    }),
  );
  await expect(page.getByRole("textbox", { name: "消息" })).toHaveValue(
    "重启也保留",
  );
  await expect(
    page.getByRole("status").filter({ hasText: "会话已失效" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "发送", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await expect
    .poll(() => page.evaluate(() => sessionStorage.getItem("alfred.session")))
    .toBe("new-session");
  expect(
    await page.evaluate(
      (session) => sessionStorage.getItem(`alfred.draft:${session}`),
      session,
    ),
  ).toBe("重启也保留");
});
