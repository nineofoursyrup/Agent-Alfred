import { test, expect } from "@playwright/test";
import {controlledTransport, emit, domain, state, run} from "./transport.js";

test("inbox viewing is read only; continue switches drafts explicitly", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await page.getByRole("textbox", { name: "消息" }).fill("原会话草稿");
  const current = await page.evaluate(() =>
    sessionStorage.getItem("alfred.session"),
  );
  await page.route("**/api/sessions?*", (route) =>
    route.fulfill({
      json: {
        sessions: [
          {
            session_id: "legacy-session",
            title: "旧日对话",
            created_at: "旧时间",
            activity_revision: 1,
          },
        ],
        non_terminal: null,
        next_cursor: null,
      },
    }),
  );
  await page.route("**/api/sessions/runs?*", (route) =>
    route.fulfill({
      json: { session_id: "legacy-session", runs: [], next_cursor: null },
    }),
  );
  await page.route("**/api/sessions/messages?*", (route) =>
    route.fulfill({
      json: {
        session_id: "legacy-session",
        title: "旧日对话",
        messages: [
          {
            role: "user",
            blocks: [{ type: "text", text: "升级前的问题" }],
            run_id: null,
          },
          {
            role: "assistant",
            blocks: [{ type: "text", text: "升级前的回复" }],
            run_id: null,
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/api/mainbar?*", (route) => {
    if (
      new URL(route.request().url()).searchParams.get("session_id") !==
      "legacy-session"
    )
      return route.continue();
    return route.fulfill({
      json: {
        items: [
          {
            type: "historic_message",
            role: "assistant",
            blocks: [{ type: "text", text: "升级前的回复" }],
            run_id: null,
          },
          {
            type: "historic_message",
            role: "user",
            blocks: [{ type: "text", text: "升级前的问题" }],
            run_id: null,
          },
        ],
        next_cursor: null,
        runs_pending: false,
      },
    });
  });
  await page.getByRole("link", { name: "收件箱", exact: true }).click();
  await page.getByRole("button", { name: "旧日对话", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "会话只读预览" }),
  ).toContainText("升级前的回复");
  expect(
    await page.evaluate(() => sessionStorage.getItem("alfred.session")),
  ).toBe(current);
  await expect(page.getByRole("textbox", { name: "消息" })).toHaveValue(
    "原会话草稿",
  );
  await page.getByRole("button", { name: "继续此会话", exact: true }).click();
  await expect(page.getByRole("region", { name: "主对话" })).toContainText(
    "升级前的问题",
  );
  await expect(page.getByRole("textbox", { name: "消息" })).toHaveValue("");
  expect(
    await page.evaluate(
      ({ current }) => sessionStorage.getItem(`alfred.draft:${current}`),
      { current },
    ),
  ).toBe("原会话草稿");
  await expect(
    page
      .getByRole("region", { name: "会话只读预览" })
      .getByRole("link", { name: "查看运行" }),
  ).toHaveCount(0);
});

test("a real v2 upgrade shows all historic messages and creates no synthetic Run", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByRole("button", { name: "升级前消息 01", exact: true })
    .click();
  const preview = page.getByRole("region", { name: "会话只读预览" });
  await expect(preview.getByRole("link", { name: "查看运行" })).toHaveCount(0);
  await page.getByRole("button", { name: "继续此会话", exact: true }).click();
  const chat = page.getByRole("region", { name: "主对话" });
  await expect(chat.getByText(/^升级前消息 \d+$/)).toHaveCount(25);
  await chat.getByRole("button", { name: "更早的消息", exact: true }).click();
  await expect(chat.getByText(/^升级前消息 \d+$/)).toHaveCount(50);
  await chat.getByRole("button", { name: "更早的消息", exact: true }).click();
  await expect(chat.getByText(/^升级前消息 \d+$/)).toHaveCount(55);
  await expect(
    chat.getByRole("button", { name: "更早的消息", exact: true }),
  ).toBeHidden();
  await expect(chat.getByText("升级前消息 01", { exact: true })).toHaveCount(1);
  await expect(chat.getByText("升级前消息 55", { exact: true })).toHaveCount(1);
});

test("MainBar paginates across the Run and historic segments without deduplicating equal messages", async ({
  page,
}) => {
  const visited = [];
  await page.route("**/api/mainbar?*", (route) => {
    const query = new URL(route.request().url()).searchParams;
    const cursor = query.get("cursor");
    visited.push(cursor);
    const pair = {
      type: "run_pair",
      run_id: "new-run",
      activity_revision: 5,
      user: [{ type: "text", text: "新的问题" }],
      assistant: [{ type: "text", text: "新的回复" }],
    };
    const old = {
      type: "historic_message",
      role: "user",
      run_id: null,
      blocks: [{ type: "text", text: "相同的旧消息" }],
    };
    return route.fulfill({
      json: cursor
        ? { items: [old, old], next_cursor: null, runs_pending: false }
        : {
            items: [pair],
            next_cursor: "opaque historic boundary",
            runs_pending: false,
          },
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  const chat = page.getByRole("region", { name: "主对话" });
  await expect(chat).toContainText("新的回复");
  await chat.getByRole("button", { name: "更早的消息", exact: true }).click();
  await expect(chat.getByText("相同的旧消息", { exact: true })).toHaveCount(2);
  await expect(chat.getByText("新的回复", { exact: true })).toHaveCount(1);
  expect(visited).toEqual([null, "opaque historic boundary"]);
});


test("pending settlement cannot rewind an already loaded historic cursor", async ({page}) => {
  let mode = "initial";
  const seen = [];
  const historic = number => ({type:"historic_message", blocks:[{type:"text", text:`历史 ${number}`} ]});
  const descending = (high, low) => Array.from({length: high-low+1}, (_,i) => historic(high-i));
  await page.route("**/api/mainbar?*", route => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    seen.push(cursor);
    const pair = {type:"run_pair", run_id:"r1", activity_revision:99, user:[], assistant:[]};
    const body = cursor ? {items: descending(Number(cursor)-1, Math.max(1, Number(cursor)-25)), next_cursor: Number(cursor)>26 ? String(Number(cursor)-25) : null, runs_pending:false}
      : mode === "initial" ? {items:descending(55,31), next_cursor:"31", runs_pending:false}
      : mode === "pending" ? {items:[], next_cursor:null, runs_pending:true}
      : {items:[pair,...descending(55,32)], next_cursor:"32", runs_pending:false};
    return route.fulfill({json:body});
  });
  const session = await controlledTransport(page);
  await emit(page, "state_patch", state(session));
  await page.getByRole("button", {name:"展开对话", exact:true}).click();
  const chat = page.getByRole("region", {name:"主对话"});
  await expect(chat.getByText(/^历史 \d+$/)).toHaveCount(25);
  mode = "pending";
  await domain(page, 1, session, {name:"run.finished", outcome:"completed", reply:{blocks:[]}});
  await expect(chat.getByRole("button", {name:"当前运行保存后继续读取"})).toBeDisabled();
  mode = "recorded";
  await emit(page, "state_patch", state(session, 2, {active_run:run(session,{phase:"finished", outcome:"completed", recording_state:"recorded"})}));
  await chat.getByRole("button", {name:"更早的消息", exact:true}).click();
  await expect(chat.getByText(/^历史 \d+$/)).toHaveCount(50);
  await expect(chat.getByText("历史 31", {exact:true})).toHaveCount(1);
  expect(seen.at(-1)).toBe("31");
});
