import {test, expect} from "@playwright/test";
import {localServer} from "./local-server.js";

test("a real upgraded Session combines a new UI Run with every historic message in order", async ({page}) => {
  const server = await localServer();
  try {
    await page.goto(server.origin);
    await page.getByRole("button", {name:"升级前消息 01",exact:true}).click();
    await page.getByRole("button", {name:"继续此会话",exact:true}).click();
    const chat = page.getByRole("region", {name:"主对话"});
    await expect(chat.getByText(/^升级前消息 \d+$/)).toHaveCount(25);
    await page.getByRole("textbox", {name:"消息"}).fill("升级后同一会话的新问题");
    const accepted = page.waitForResponse(response => response.url().endsWith("/api/runs") && response.request().method() === "POST");
    await page.getByRole("button", {name:"发送",exact:true}).click();
    const response = await accepted;
    expect(response.status()).toBe(202);
    expect((await response.json()).session_id).toBe("legacy /会话?");
    await expect(chat).toContainText("已保存");
    await chat.getByRole("button", {name:"更早的消息",exact:true}).click();
    await expect(chat.getByText(/^升级前消息 \d+$/)).toHaveCount(50);
    await chat.getByRole("button", {name:"更早的消息",exact:true}).click();
    const expected = [...Array.from({length:55},(_,i)=>`升级前消息 ${String(i+1).padStart(2,"0")}`),"升级后同一会话的新问题","离线模型回复","Skill 已降级：部分流程未使用或选择器不可用；请查看运行详情中的本次输入。"];
    await expect(chat.locator("#messages > p")).toHaveText(expected);
    await expect(chat.getByRole("button", {name:"更早的消息",exact:true})).toBeHidden();
    await page.reload();
    await expect(chat.getByText("离线模型回复",{exact:true})).toHaveCount(1);
    await chat.getByRole("button", {name:"更早的消息",exact:true}).click();
    await expect(chat.getByText(/^升级前消息 \d+$/)).toHaveCount(49);
    await chat.getByRole("button", {name:"更早的消息",exact:true}).click();
    await expect(chat.locator("#messages > p")).toHaveText(expected);
  } finally {
    await server.close();
  }
});

test("real migrated null-run messages create no Run rows before or after a new UI Run", async ({page}) => {
  const server = await localServer();
  try {
    await page.goto(`${server.origin}/runs?filter=all`);
    const list = page.getByRole("region",{name:"运行列表"});
    await expect(page.getByRole("heading",{name:"运行详情",exact:true})).toBeVisible();
    const before = await (await page.request.get(`${server.origin}/api/runs?filter=all`)).json();
    expect(before.runs).toEqual([]);
    expect(before.non_terminal).toBeNull();
    await expect(list.locator("article")).toHaveCount(0);
    await page.getByRole("link",{name:"收件箱",exact:true}).click();
    await page.getByRole("button",{name:"升级前消息 01",exact:true}).click();
    await page.getByRole("button",{name:"继续此会话",exact:true}).click();
    await expect(page.getByRole("region",{name:"主对话"}).getByText(/^升级前消息 \d+$/)).toHaveCount(25);
    const messages = await (await page.request.get(`${server.origin}/api/sessions/messages?`+new URLSearchParams({session_id:"legacy /会话?",page_size:"100"}))).json();
    expect(messages.messages).toHaveLength(55);
    expect(messages.messages.every(message=>message.run_id===null)).toBe(true);
    await page.getByRole("textbox",{name:"消息"}).fill("唯一真正新增的 Run");
    const accepted = page.waitForResponse(response=>response.url().endsWith("/api/runs")&&response.status()===202);
    await page.getByRole("button",{name:"发送",exact:true}).click();
    const {run_id} = await (await accepted).json();
    await expect(page.getByRole("region",{name:"主对话"})).toContainText("已保存");
    await page.getByRole("link",{name:"运行",exact:true}).click();
    await expect(list.locator("article")).toHaveCount(1);
    await expect(list.getByRole("link",{name:"查看运行",exact:true})).toHaveAttribute("href",`/runs/${encodeURIComponent(run_id)}?filter=chat`);
    const after = await (await page.request.get(`${server.origin}/api/runs?filter=all`)).json();
    expect(after.runs.map(run=>run.run_id)).toEqual([run_id]);
    expect(after.non_terminal).toBeNull();
  } finally {
    await server.close();
  }
});
