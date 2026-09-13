import {test, expect} from "@playwright/test";
import {mkdir, writeFile} from "node:fs/promises";
import {join} from "node:path";
import {api, memoryServer} from "./memory-server.js";

async function prepare(directory) {
  for (const [name, body] of [["A", "历史 A 完整流程"], ["B", "长".repeat(8001)], ["C", "C 完整流程"]]) {
    const path = join(directory, "builtin", name);
    await mkdir(path, {recursive: true});
    await writeFile(join(path, "SKILL.md"), `---\nname: ${name}\ndescription: ${name}流程\n---\n${body}`);
  }
}
async function chat(page, origin) {
  await page.goto(origin + "/inbox");
  const [created] = await Promise.all([
    page.waitForResponse(r => r.url().endsWith("/api/sessions") && r.request().method() === "POST"),
    page.getByRole("button", {name: "新建会话", exact: true}).click(),
  ]);
  expect(created.status()).toBe(201);
  const session = (await created.json()).session_id;
  await page.waitForFunction(id => sessionStorage.getItem("alfred.session") === id, session);
  const expand = page.getByRole("button", {name: "展开对话", exact: true});
  if (await expand.isVisible()) await expand.click();
}
async function send(page, text) {
  await page.getByRole("textbox", {name: "消息"}).fill(text);
  await page.getByRole("button", {name: "发送", exact: true}).click();
}
async function runFor(other, prompt) {
  let found;
  await expect.poll(async () => {
    const {status, body} = await other.get("/api/runs?filter=chat&limit=25");
    expect(status).toBe(200);
    found = body.runs.find(r => r.prompt_preview === prompt && r.phase === "finished");
    return found?.run_id;
  }).toBeTruthy();
  return found.run_id;
}

test("CE-10/22/28: real Skill preparation, attempts, MainBar and historical metadata", async ({page}) => {
  const server = await memoryServer({script: "tests/browser/skills_server.py", prepare});
  try {
    const other = await api(page.request, server.origin);
    await chat(page, server.origin);
    await send(page, "自动整理");
    const run = await runFor(other, "自动整理");
    await expect(page.getByRole("region", {name: "主对话"}).getByText(/Skill 已降级/)).toBeVisible();
    await page.goto(`${server.origin}/runs/${run}`);
    const input = page.locator('details[data-attempt="input-explanation"]');
    await input.locator("summary").click();
    await expect(input.getByText(/选择方式：model/)).toBeVisible();
    await expect(input.getByText(/B.*body_limit_exceeded/)).toBeVisible();
    await expect(input.getByText(/实际请求携带 Skill A/)).toBeVisible();
    await expect(input.getByText(/实际请求携带 Skill C/)).toBeVisible();
    await writeFile(join(server.directory, "builtin", "A", "SKILL.md"), "---\nname: A\ndescription: 新版本\n---\n重启新正文");
    await server.restart();
    await page.reload();
    await input.locator("summary").click();
    await expect(input.getByText(/实际请求携带 Skill A/)).toBeVisible();
    await expect(input.getByText("重启新正文")).toHaveCount(0);
    await chat(page, server.origin);
    await send(page, "/skills Missing\n整理");
    await expect(page.getByRole("region", {name: "主对话"}).getByText(/Skill 准备失败/)).toBeVisible();
  } finally { await server.close(); }
});

test("CE-23: a late real evidence response cannot replace the next Run", async ({page}) => {
  const server = await memoryServer({script: "tests/browser/skills_server.py", prepare});
  let release;
  const held = new Promise(resolve => {release = resolve;});
  let observed;
  const intercepted = new Promise(resolve => {observed = resolve;});
  let delivered;
  const complete = new Promise(resolve => {delivered = resolve;});
  try {
    const other = await api(page.request, server.origin);
    await chat(page, server.origin);
    await send(page, "A run automatic");
    const first = await runFor(other, "A run automatic");
    await send(page, "/skills off\nB run disabled");
    const second = await runFor(other, "/skills off\nB run disabled");
    await page.route(`**/api/run-evidence?run_id=${first}`, async route => {
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      expect((await response.json()).run_id).toBe(first);
      observed();
      await held;
      await route.fulfill({response});
      delivered();
    });
    await page.goto(`${server.origin}/runs/${first}`, {waitUntil: "domcontentloaded"});
    await intercepted;
    await page.locator(`a[href^="/runs/${second}"]`).click();
    const input = page.locator('details[data-attempt="input-explanation"]');
    await input.locator("summary").click();
    await expect(input.getByText(/选择方式：disabled/)).toBeVisible();
    release();
    await complete;
    await expect(input.getByText(/选择方式：disabled/)).toBeVisible();
    await expect(input.getByText(/body_limit_exceeded/)).toHaveCount(0);
    await page.reload();
    await input.locator("summary").click();
    await expect(input.getByText(/选择方式：disabled/)).toBeVisible();
  } finally { release(); await server.close(); }
});

test("CE-19/21: selector input failure has no fictitious Step or Attempt", async ({page}) => {
  const server = await memoryServer({script: "tests/browser/skills_server.py", prepare});
  try {
    await chat(page, server.origin);
    const accepted = page.waitForResponse(r => r.url().endsWith("/api/runs") && r.request().method() === "POST");
    await send(page, "x".repeat(64001));
    const response = await accepted;
    expect(response.status()).toBe(202);
    const {run_id} = await response.json();
    await expect(page.getByRole("region", {name: "主对话"})).toContainText("输入超限");
    await page.goto(`${server.origin}/runs/${run_id}`);
    const detail = page.getByRole("region", {name: "运行过程"});
    await detail.getByText("本次输入", {exact: true}).click();
    await expect(detail).toContainText("输入准备失败 · Skill 选择器");
    await expect(detail).not.toContainText("undefined");
    await expect(detail).not.toContainText("Attempt");
    await page.reload();
    await detail.getByText("本次输入", {exact: true}).click();
    await expect(detail).toContainText("输入准备失败 · Skill 选择器");
    await expect(detail).not.toContainText("undefined");
  } finally { await server.close(); }
});
