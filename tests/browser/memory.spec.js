import {test, expect} from "@playwright/test";
import {api, memoryServer} from "./memory-server.js";
import {controlledEventSource, emit, state} from "./transport.js";

// Records, from this point on, whether a text was ever rendered and whether
// it came back after having been hidden -- not only whether it is absent now.
async function watchText(page, text) {
  await page.evaluate(text => {
    const seen = window.watched = {shown: false, hidden: false, returned: false};
    new MutationObserver(() => {
      if (!document.body.textContent.includes(text)) seen.hidden = true;
      else {
        seen.shown = true;
        if (seen.hidden) seen.returned = true;
      }
    }).observe(document.body, {childList: true, subtree: true, characterData: true});
  }, text);
  return () => page.evaluate(() => window.watched);
}

test("A40: empty, no match, read failure, busy and conflicts stay distinct", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await expect(panel.getByText("记忆库为空。", {exact: true})).toBeVisible();

    await panel.getByLabel("新主题").fill("饮食");
    await panel.getByLabel("新事实").fill("Zephyr 喜欢香菜");
    await server.send("busy");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await expect(panel.getByText(/宿主正忙.*不会排队.*草稿已保留/)).toBeVisible();
    await expect(panel.getByLabel("新事实")).toHaveValue("Zephyr 喜欢香菜");
    await server.send("idle");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await expect(panel.getByText(/^已保存（ID .+，版本 1）。$/)).toBeVisible();
    const list = panel.getByLabel("语义记忆列表");
    await expect(list.getByText("Zephyr 喜欢香菜")).toBeVisible();

    await panel.getByLabel("文本").fill("不存在的词");
    await panel.getByRole("button", {name: "搜索", exact: true}).click();
    await expect(panel.getByText("没有匹配的记忆。", {exact: true})).toBeVisible();

    await page.route("**/api/memory/records?*", route =>
      route.fulfill({status: 503, json: {error: {code: "storage_read_failed"}}}));
    await panel.getByRole("button", {name: "显示全部", exact: true}).click();
    await expect(panel.getByText(/读取失败（storage_read_failed）.*未显示旧列表/)).toBeVisible();
    await expect(list.getByText("Zephyr 喜欢香菜")).toHaveCount(0);
    await expect(panel.getByText("记忆库为空。", {exact: true})).toHaveCount(0);
    await page.unroute("**/api/memory/records?*");
    await panel.getByRole("button", {name: "从首页重新读取", exact: true}).click();
    await expect(list.getByText("Zephyr 喜欢香菜")).toBeVisible();

    await list.getByRole("button", {name: "查看详情", exact: true}).click();
    const detail = panel.getByLabel("语义记忆详情");
    await expect(detail.getByText("来源组：无历史来源（手动录入）")).toBeVisible();
    const id = await list.locator("article").first().getAttribute("data-memory-id");
    await detail.getByRole("button", {name: "编辑", exact: true}).click();
    const draft = panel.getByLabel("事实", {exact: true});
    await draft.fill("我的未提交草稿");
    const edited = await other.command({
      operation_id: "other-tab-edit", kind: "semantic", action: "update",
      payload: {id, fact: "另一标签页的新内容"}, expected_version: 1,
    });
    expect(edited.status).toBe(200);
    await expect(detail.getByText("另一标签页的新内容")).toBeVisible();
    await expect(panel.getByText(/当前记录已是版本 2，草稿基于版本 1/)).toBeVisible();
    await expect(draft).toHaveValue("我的未提交草稿");
    await panel.getByRole("button", {name: "保存修改", exact: true}).click();
    await expect(panel.getByText(/版本冲突：记录当前为版本 2，草稿基于版本 1；草稿已保留/)).toBeVisible();
    await expect(draft).toHaveValue("我的未提交草稿");
    await panel.getByRole("button", {name: "基于当前版本继续编辑", exact: true}).click();
    await panel.getByRole("button", {name: "保存修改", exact: true}).click();
    await expect(detail.getByText("已更新到版本 3。")).toBeVisible();
    await expect(detail.getByText("我的未提交草稿")).toBeVisible();

    await detail.getByRole("button", {name: "编辑", exact: true}).click();
    await draft.fill("删除前的草稿");
    const removed = await other.command({
      operation_id: "other-tab-delete", kind: "semantic", action: "delete",
      payload: {id}, expected_version: 3,
    });
    expect(removed.body.result.status).toBe("deleted");
    await expect(detail.getByText("记录已不存在，编辑草稿已清除。")).toBeVisible();
    await expect(draft).toBeHidden();
    await expect(draft).toHaveValue("");
    await expect(panel.getByText("我的未提交草稿")).toHaveCount(0);
    await expect(panel.getByText("记忆库为空。", {exact: true})).toBeVisible();
  } finally {
    await server.close();
  }
});

async function chatRun(other, prompt) {
  let found;
  await expect.poll(async () => {
    const {body} = await other.get("/api/runs?filter=chat&limit=25");
    found = body.runs.find(run => run.prompt_preview === prompt && run.phase === "finished");
    return Boolean(found);
  }).toBe(true);
  return found.run_id;
}

test("A52: explicit save, next-Run hit, source, edit conflict, delete and requery", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    await page.goto(server.origin + "/inbox");
    await page.getByRole("button", {name: "新建会话", exact: true}).click();
    await page.getByRole("button", {name: "展开对话", exact: true}).click();
    const chat = page.getByRole("region", {name: "主对话"});
    const send = async text => {
      await page.getByRole("textbox", {name: "消息"}).fill(text);
      await page.getByRole("button", {name: "发送", exact: true}).click();
    };
    await send("请记住 Zephyr：Zephyr likes coriander");
    await expect(chat).toContainText("已记住。");
    await expect(chat).toContainText('"status": "saved"');
    const saveRun = await chatRun(other, "请记住 Zephyr：Zephyr likes coriander");
    await send("Zephyr likes what");
    const hitRun = await chatRun(other, "Zephyr likes what");

    await page.goto(`${server.origin}/runs/${encodeURIComponent(hitRun)}?filter=chat`);
    const process = page.getByRole("region", {name: "运行过程"});
    await process.getByText("本次输入", {exact: true}).click();
    await expect(process.getByText(/^结果 hit · 模型判断 · 原因 personal_information$/)).toBeVisible();
    await expect(process.getByText(/召回 1 条 · 选入参考资料 1 条 · 输入处置 ready/)).toBeVisible();
    await expect(process.getByText(/语义 第 1 条 · .+ · 引用版本 1 · 工具写入 · 调用 memory-save · 已选入/)).toBeVisible();
    await expect(process.getByText(/实际请求携带 语义记忆 .+ · 版本 1/)).toBeVisible();
    await expect(process).not.toContainText("bm25");
    await process.getByRole("button", {name: "查看当前内容"}).first().click();
    await expect(process.getByText("当前内容与引用版本一致").first()).toBeVisible();
    await expect(process.getByText("Zephyr：Zephyr likes coriander").first()).toBeVisible();
    await expect(process.getByRole("link", {name: `Run ${saveRun}`, exact: true}).first()).toBeVisible();

    await page.getByRole("link", {name: "记忆", exact: true}).click();
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    const list = panel.getByLabel("语义记忆列表");
    await expect(list.getByText("Zephyr likes coriander")).toBeVisible();
    await expect(list).toContainText("创建：工具写入 · 调用 memory-save");
    await expect(list).toContainText("人工保护");
    await list.getByRole("button", {name: "查看详情", exact: true}).click();
    const detail = panel.getByLabel("语义记忆详情");
    const id = await list.locator("article").first().getAttribute("data-memory-id");
    await expect(detail.getByRole("link", {name: `Run ${saveRun}`, exact: true})).toBeVisible();

    await detail.getByRole("button", {name: "编辑", exact: true}).click();
    await panel.getByLabel("事实", {exact: true}).fill("Zephyr likes basil");
    expect((await other.command({
      operation_id: "second-tab", kind: "semantic", action: "update",
      payload: {id, fact: "Zephyr likes cilantro"}, expected_version: 1,
    })).status).toBe(200);
    await expect(detail.getByText("Zephyr likes cilantro")).toBeVisible();
    await panel.getByRole("button", {name: "保存修改", exact: true}).click();
    await expect(panel.getByText(/版本冲突：记录当前为版本 2/)).toBeVisible();
    await expect(panel.getByLabel("事实", {exact: true})).toHaveValue("Zephyr likes basil");
    await panel.getByRole("button", {name: "放弃草稿", exact: true}).click();

    await detail.getByRole("button", {name: "删除", exact: true}).click();
    await expect(detail.getByText(/确认删除版本 2？/)).toBeVisible();
    await detail.getByRole("button", {name: "确认删除", exact: true}).click();
    await expect(detail.getByText("记忆已删除；遗忘进度见操作回执。")).toBeVisible();
    const receipts = page.getByRole("region", {name: "记忆操作回执"});
    await expect(receipts.getByText(`记忆已删除（数据库已确认） · ID ${id}`, {exact: false})).toBeVisible();
    await expect(receipts.getByText("遗忘完成：条目、索引与受管副本均已清理并核实。")).toBeVisible();
    await expect(receipts).toContainText("原始会话与删除前的追踪仍可人工查看");
    await expect(receipts).not.toContainText("cilantro");

    await panel.getByLabel("文本").fill("Zephyr");
    await panel.getByRole("button", {name: "搜索", exact: true}).click();
    await expect(panel.getByText("没有匹配的记忆。", {exact: true})).toBeVisible();
    expect((await other.get(`/api/memory/record?kind=semantic&id=${id}`)).status).toBe(404);
    const mirrors = page.getByRole("region", {name: "Markdown 镜像"});
    await mirrors.locator('[data-mirror="facts"]').getByRole("button", {name: "预览"}).click();
    await expect(mirrors.locator('[data-mirror="facts"] pre')).toContainText("# Facts");
    await expect(mirrors).not.toContainText("Zephyr");
    const {readFile} = await import("node:fs/promises");
    expect(await readFile(`${server.directory}/memory/facts.md`, "utf8")).not.toContain("Zephyr");

    // The drawer stays expanded for this tab; raw history is still readable.
    await page.goto(server.origin + "/inbox");
    await expect(chat).toContainText("请记住 Zephyr：Zephyr likes coriander");
    await send("Zephyr likes what now");
    const missRun = await chatRun(other, "Zephyr likes what now");
    await page.goto(`${server.origin}/runs/${encodeURIComponent(missRun)}?filter=chat`);
    await process.getByText("本次输入", {exact: true}).click();
    await expect(process.getByText(/^结果 miss · 模型判断/)).toBeVisible();
    await expect(process.getByText(/召回 0 条 · 选入参考资料 0 条/)).toBeVisible();
    // Forgetting isolated the source Run and its reader from automatic input.
    await expect(process).toContainText(/历史排除：不完整 0，隔离或来源未确认 2，/);
    await expect(process).not.toContainText(`历史 Run ${saveRun}`);
    await expect(process).not.toContainText(`历史 Run ${hitRun}`);
    await expect(process).not.toContainText(`来源 Run ${saveRun}`);
    await expect(process).not.toContainText("coriander");

    await page.getByRole("link", {name: "记忆", exact: true}).click();
    const statistics = page.getByRole("region", {name: "检索统计"});
    await statistics.getByRole("button", {name: "刷新统计", exact: true}).click();
    await expect(statistics).toContainText("全部会话的已记录对话运行");
    await expect(statistics).toContainText("跳过 S/(S+H+M) 0/3 = 0.0%");
    await expect(statistics).toContainText("命中 H/(H+M) 1/3 = 33.3%");
    await expect(statistics).toContainText("错误 E/(S+H+M+E) 0/3 = 0.0%");
    await statistics.getByLabel("仅当前会话").check();
    await expect(statistics).toContainText("当前会话的已记录对话运行");
    await expect(statistics).toContainText("命中 H/(H+M) 1/3 = 33.3%");

    await page.goto(`${server.origin}/runs/${encodeURIComponent(hitRun)}?filter=chat`);
    await process.getByText("本次输入", {exact: true}).click();
    await process.getByRole("button", {name: "查看当前内容"}).first().click();
    await expect(process.getByText("记录已不存在（可能已被删除）；不显示旧正文。").first()).toBeVisible();
    await expect(process).not.toContainText("likes coriander");
  } finally {
    await server.close();
  }
});

test("A35: a delete in one tab clears the other tab and late reads never restore the body", async ({page, context}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({
      operation_id: "seed", kind: "semantic", action: "save",
      payload: {subject: "Zephyr", fact: "Zephyr likes coriander"},
    });
    const id = saved.body.result.memory_id;
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await panel.getByLabel("文本").fill("Zephyr");
    await panel.getByRole("button", {name: "搜索", exact: true}).click();
    const list = panel.getByLabel("语义记忆列表");
    await list.getByRole("button", {name: "查看详情", exact: true}).click();
    const detail = panel.getByLabel("语义记忆详情");
    await detail.getByRole("button", {name: "编辑", exact: true}).click();
    await panel.getByLabel("事实", {exact: true}).fill("草稿 coriander basil");

    // Two old reads are answered by the server now but delivered only later.
    const held = [];
    let holding = true;
    const captured = new Promise(resolve => {
      page.route(/\/api\/memory\/(record|records)\?/, async route => {
        if (!holding) return route.continue();
        held.push({route, response: await route.fetch()});
        if (held.length === 2) {
          holding = false;
          resolve();
        }
      });
    });
    await list.getByRole("button", {name: "查看详情", exact: true}).click();
    await panel.getByRole("button", {name: "搜索", exact: true}).click();
    await captured;

    const second = await context.newPage();
    await second.goto(server.origin + "/memory");
    const secondPanel = second.getByRole("tabpanel", {name: "语义记忆"});
    await secondPanel.getByRole("button", {name: "查看详情", exact: true}).click();
    await secondPanel.getByLabel("语义记忆详情").getByRole("button", {name: "删除", exact: true}).click();
    await secondPanel.getByRole("button", {name: "确认删除", exact: true}).click();
    await expect(secondPanel.getByText("记忆已删除；遗忘进度见操作回执。")).toBeVisible();

    await expect(detail.getByText("记录已不存在（可能已被删除）；不显示旧正文。")).toBeVisible();
    await expect(panel.getByText("没有匹配的记忆。", {exact: true})).toBeVisible();
    await expect(panel.getByLabel("事实", {exact: true})).toBeHidden();
    await expect(page.getByText("coriander")).toHaveCount(0);

    const watched = await watchText(page, "coriander");
    for (const {route, response} of held) {
      const finished = page.waitForEvent("requestfinished", request => request === route.request());
      await route.fulfill({response});
      await finished;
    }
    // The race itself is fixed by the held routes; these yields only let the
    // page finish consuming both released bodies before the observer is read.
    await page.evaluate(async () => {
      for (let i = 0; i < 5; i++) await new Promise(resolve => setTimeout(resolve, 0));
    });
    expect((await watched()).shown).toBe(false);
    await expect(page.getByText("coriander")).toHaveCount(0);
    await expect(detail.getByText("记录已不存在（可能已被删除）；不显示旧正文。")).toBeVisible();
    expect((await other.get(`/api/memory/record?kind=semantic&id=${id}`)).status).toBe(404);
    // Manual changes produced no Run: invalidation did not need a fake Run.
    expect((await other.get("/api/runs?filter=all&limit=25")).body.runs).toEqual([]);
  } finally {
    await server.close();
  }
});

async function controlledStream(page, origin) {
  await controlledEventSource(page);
  const {instance_id: instance} = await (await page.request.get(origin + "/api/entry")).json();
  let stateRevision = 0;
  return {
    instance,
    emit: (kind, body) => emit(page, kind, body),
    async connect() {
      await emit(page, "open", {});
      await emit(page, "state_patch", state(null, ++stateRevision, {process_instance_id: instance}));
    },
    patch(revision, process = instance) {
      return emit(page, "memory_patch", {
        schema_version: 1, process_instance_id: process, memory_revision: revision, change: null,
      });
    },
  };
}

test("A36/A55: disconnect hides bodies, reconnect verifies, and stale or foreign frames are ignored", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({
      operation_id: "seed", kind: "semantic", action: "save",
      payload: {subject: "Zephyr", fact: "Zephyr likes coriander"},
    });
    const id = saved.body.result.memory_id;
    const stream = await controlledStream(page, server.origin);
    await page.goto(server.origin + "/memory");
    await stream.connect();
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    const list = panel.getByLabel("语义记忆列表");
    await list.getByRole("button", {name: "查看详情", exact: true}).click();
    const detail = panel.getByLabel("语义记忆详情");
    await expect(detail.getByText("Zephyr likes coriander")).toBeVisible();
    await detail.getByRole("button", {name: "编辑", exact: true}).click();
    await panel.getByLabel("事实", {exact: true}).fill("离线前的草稿");
    const facts = page.getByRole("region", {name: "Markdown 镜像"}).locator('[data-mirror="facts"]');
    await facts.getByRole("button", {name: "预览", exact: true}).click();
    await expect(facts.locator("pre")).toContainText("Zephyr likes coriander");

    const reads = [];
    page.on("request", request => {
      if (/\/api\/memory\/(state|records?)(\?|$)/.test(request.url())) reads.push(request.url());
    });
    const {memory_revision: current} = (await other.get("/api/memory/state")).body;
    await stream.patch(current + 50, "an-older-process");
    await stream.patch(current - 1);
    await stream.patch(current);
    // Frames are dispatched synchronously; one yield lets any read they would
    // start be issued before asserting that none was.
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
    expect(reads).toEqual([]);
    await expect(detail.getByText("Zephyr likes coriander")).toBeVisible();

    await stream.emit("error", null);
    await expect(page.getByText(/^连接中断：记忆正文、候选与镜像预览已隐藏/)).toBeVisible();
    await expect(page.getByText("连接中断：镜像预览已隐藏，重连并核验后再显示。")).toBeVisible();
    await expect(panel.getByText("连接中断：记忆正文已隐藏，重连并核验后再显示。")).toBeVisible();
    await expect(page.getByText("Zephyr likes coriander")).toHaveCount(0);
    await expect(panel.getByLabel("事实", {exact: true})).toBeHidden();
    const watched = await watchText(page, "coriander");
    // Undeliverable while offline: the record is deleted with no frame.
    expect((await other.command({
      operation_id: "offline-delete", kind: "semantic", action: "delete",
      payload: {id}, expected_version: 1,
    })).body.result.status).toBe("deleted");
    // A frame after the disconnect is not from a live stream: it is ignored
    // and cannot inflate the revision the reconnect verifies against.
    await stream.patch(current + 1000);
    await expect(page.getByText("Zephyr likes coriander")).toHaveCount(0);

    await stream.connect();
    await expect(detail.getByText("记录已不存在，编辑草稿已清除。")).toBeVisible();
    await expect(panel.getByText("记忆库为空。", {exact: true})).toBeVisible();
    await expect(panel.getByLabel("事实", {exact: true})).toHaveValue("");
    await expect(page.getByText("coriander")).toHaveCount(0);
    expect((await watched()).shown).toBe(false);
    expect(reads.some(url => url.includes("/api/memory/state"))).toBe(true);

    // A reconnect whose revision cannot be read stays hidden until re-verified.
    await other.command({
      operation_id: "after", kind: "semantic", action: "save",
      payload: {subject: "Zephyr", fact: "Zephyr likes basil"},
    });
    await page.route("**/api/memory/state", route =>
      route.fulfill({status: 503, json: {error: {code: "storage_read_failed"}}}));
    await stream.emit("error", null);
    await stream.connect();
    await expect(page.getByText("记忆状态核验失败：正文已隐藏，可重新核验。")).toBeVisible();
    await expect(page.getByText("Zephyr likes basil")).toHaveCount(0);
    await page.unroute("**/api/memory/state");
    await page.getByRole("button", {name: "重新核验", exact: true}).click();
    await expect(list.getByText("Zephyr likes basil")).toBeVisible();
    await expect(page.getByRole("button", {name: "重新核验", exact: true})).toBeHidden();
  } finally {
    await server.close();
  }
});

test("A37: expanded gate references show the edited version, absence and read faults distinctly", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({
      operation_id: "seed", kind: "semantic", action: "save",
      payload: {subject: "Zephyr", fact: "Zephyr likes coriander"},
    });
    const id = saved.body.result.memory_id;
    await page.goto(server.origin + "/inbox");
    await page.getByRole("button", {name: "新建会话", exact: true}).click();
    await page.getByRole("textbox", {name: "消息"}).fill("Zephyr likes what");
    await page.getByRole("button", {name: "发送", exact: true}).click();
    const run = await chatRun(other, "Zephyr likes what");
    expect((await other.command({
      operation_id: "edit", kind: "semantic", action: "update",
      payload: {id, fact: "Zephyr likes cilantro"}, expected_version: 1,
    })).status).toBe(200);

    await page.goto(`${server.origin}/runs/${encodeURIComponent(run)}?filter=chat`);
    const process = page.getByRole("region", {name: "运行过程"});
    await process.getByText("本次输入", {exact: true}).click();
    await expect(process.getByText(/语义 第 1 条 · .+ · 引用版本 1 · 用户手填（Web） · 已选入/)).toBeVisible();
    await process.getByRole("button", {name: "查看当前内容"}).first().click();
    await expect(process.getByText("当前展示为修改后内容（当前版本 2，引用时版本 1）").first()).toBeVisible();
    await expect(process.getByText("Zephyr：Zephyr likes cilantro").first()).toBeVisible();
    await expect(process).not.toContainText("likes coriander");

    await page.route("**/api/memory/record?*", route =>
      route.fulfill({status: 503, json: {error: {code: "storage_read_failed"}}}));
    await process.getByRole("button", {name: "收起当前内容"}).first().click();
    await process.getByRole("button", {name: "查看当前内容"}).first().click();
    await expect(process.getByText("读取失败（storage_read_failed）：不能确认记录是否存在。").first()).toBeVisible();
    await expect(process.getByText(/记录已不存在/)).toHaveCount(0);
    await expect(process).not.toContainText("likes cilantro");
    await page.unroute("**/api/memory/record?*");

    expect((await other.command({
      operation_id: "delete", kind: "semantic", action: "delete",
      payload: {id}, expected_version: 2,
    })).status).toBe(200);
    await expect(process.getByText("记录已不存在（可能已被删除）；不显示旧正文。").first()).toBeVisible();
    await expect(process).not.toContainText("likes cilantro");
    await expect(process).not.toContainText(/bm25|relevance|相关度/);
  } finally {
    await server.close();
  }
});

const SEED_SAME_INSTANT = `
import sqlite3, sys, threading
from datetime import UTC, datetime
from pathlib import Path
sys.path.insert(0, "src")
from agent_alfred import schema
from agent_alfred.memory.audit import AuditKey
from agent_alfred.memory.commands import CommandContext, MemoryCommandService
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.redact import Redactor
from agent_alfred.runtime.recording import RecordingStore
state = Path(sys.argv[1])
conn = sqlite3.connect(state / "db.sqlite3")
schema.migrate(conn)
instant = datetime(2026, 9, 1, tzinfo=UTC)
memory = MemoryCommandService(
    recording_store=RecordingStore(conn, threading.Lock()),
    audit_key=AuditKey.load_or_create(state / "audit.key", Redactor(())),
    clock=lambda: instant,
)
for index in range(30):
    receipt = memory.execute({"operation_id": f"seed-{index}", "kind": "semantic",
        "action": "save", "payload": {"subject": "同时", "fact": f"同一时刻 {index:02}"}},
        CommandContext(ManualOrigin("web"), "web"))
    assert receipt["status"] == "saved", receipt
conn.close()
`;

test("A38: equal creation times page in stable internal order and stale cursors force a reread", async ({page}) => {
  const {execFileSync} = await import("node:child_process");
  const server = await memoryServer({
    prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", SEED_SAME_INSTANT, directory]),
  });
  try {
    const other = await api(page.request, server.origin);
    const stream = await controlledStream(page, server.origin);
    await page.goto(server.origin + "/memory");
    await stream.connect();
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    const list = panel.getByLabel("语义记忆列表");
    const facts = () => list.locator("article > p").allTextContents();
    await expect(list.locator("article")).toHaveCount(25);
    const expected = Array.from({length: 30}, (_, index) => `同一时刻 ${String(29 - index).padStart(2, "0")}`);
    expect(await facts()).toEqual(expected.slice(0, 25));
    for (const meta of await list.locator("article > small").allTextContents())
      expect(meta).toContain("创建：用户手填（Web） · 2026-09-01T00:00:00+00:00");
    const ids = await list.locator("article").evaluateAll(items => items.map(item => item.dataset.memoryId));
    expect(ids).not.toEqual([...ids].sort());
    await panel.getByRole("button", {name: "加载更多", exact: true}).click();
    await expect(list.locator("article")).toHaveCount(30);
    expect(await facts()).toEqual(expected);
    await expect(panel.getByRole("button", {name: "加载更多", exact: true})).toBeHidden();

    await panel.getByLabel("主题（精确匹配）").fill("同时");
    await panel.getByRole("button", {name: "搜索", exact: true}).click();
    await expect(list.locator("article")).toHaveCount(25);
    // No memory_patch reaches this controlled stream: the cursor itself must refuse.
    expect((await other.command({
      operation_id: "late", kind: "semantic", action: "save",
      payload: {subject: "同时", fact: "同一时刻 晚到"},
    })).status).toBe(200);
    await panel.getByRole("button", {name: "加载更多", exact: true}).click();
    await expect(panel.getByText("列表已变化，分页游标已失效：请从首页重新读取。")).toBeVisible();
    await expect(list.locator("article")).toHaveCount(0);
    await panel.getByRole("button", {name: "从首页重新读取", exact: true}).click();
    await expect(list.locator("article")).toHaveCount(25);
    await panel.getByRole("button", {name: "加载更多", exact: true}).click();
    await expect(list.locator("article")).toHaveCount(31);
    expect(new Set(await facts()).size).toBe(31);
  } finally {
    await server.close();
  }
});

test("A41/A42: the read-only Skill catalog shows overrides and reloads only after restart", async ({page}) => {
  const {mkdir, writeFile} = await import("node:fs/promises");
  const skill = (directory, version) => writeFile(
    `${directory}/skills/mine/SKILL.md`,
    `---\nname: browser-plan\ndescription: 用户计划\n---\n用户正文 ${version}\n  保留缩进`,
  );
  const server = await memoryServer({
    prepare: async directory => {
      await mkdir(`${directory}/skills/mine`, {recursive: true});
      await mkdir(`${directory}/skills/extra`, {recursive: true});
      await skill(directory, "v1");
      await writeFile(`${directory}/skills/extra/SKILL.md`, "---\nname: user-only\ndescription: 仅用户\n---\n仅用户正文");
    },
  });
  try {
    await page.goto(server.origin + "/memory?tab=skills");
    const panel = page.getByRole("tabpanel", {name: "Skill 目录"});
    await expect(panel.getByText(/只读目录：启动时建立索引，文件修改需重启后生效/)).toBeVisible();
    const plan = panel.locator("article", {hasText: "browser-plan"});
    await expect(plan).toContainText("用户 Skill · 已整体覆盖同名内置 Skill");
    await expect(panel.locator("article", {hasText: "user-only"})).toContainText("用户 Skill");
    await expect(panel.locator("article", {hasText: "user-only"})).not.toContainText("覆盖");
    await expect(panel.getByRole("button", {name: /编辑|删除|注入|匹配|新建/})).toHaveCount(0);
    await expect(panel).not.toContainText(/已匹配|已注入/);
    await plan.getByRole("button", {name: "查看正文", exact: true}).click();
    await expect(plan.locator("pre")).toHaveText("用户正文 v1\n  保留缩进");
    await expect(panel).not.toContainText("内置正文");

    await skill(server.directory, "v2");
    await plan.getByRole("button", {name: "查看正文", exact: true}).click();
    await expect(plan.locator("pre")).toHaveText("用户正文 v1\n  保留缩进");
    await page.reload();
    await page.getByRole("tabpanel", {name: "Skill 目录"}).locator("article", {hasText: "browser-plan"})
      .getByRole("button", {name: "查看正文", exact: true}).click();
    await expect(panel.locator("article", {hasText: "browser-plan"}).locator("pre")).toHaveText("用户正文 v1\n  保留缩进");

    await server.restart();
    await page.reload();
    await panel.locator("article", {hasText: "browser-plan"}).getByRole("button", {name: "查看正文", exact: true}).click();
    await expect(panel.locator("article", {hasText: "browser-plan"}).locator("pre")).toHaveText("用户正文 v2\n  保留缩进");
  } finally {
    await server.close();
  }
});

test("A47: queue states and actions are explicit; busy is 409 and nothing bypasses the threshold", async ({page}) => {
  test.setTimeout(60000);
  const server = await memoryServer({threshold: 2});
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({
      operation_id: "protected", kind: "semantic", action: "save",
      payload: {subject: "饮食", fact: "Zephyr likes coriander"},
    });
    const id = saved.body.result.memory_id;
    await server.send("plan update");
    await page.goto(server.origin + "/inbox");
    await page.getByRole("button", {name: "新建会话", exact: true}).click();
    const send = async text => {
      await page.getByRole("link", {name: "收件箱", exact: true}).click();
      await page.getByRole("textbox", {name: "消息"}).fill(text);
      await expect(page.getByRole("button", {name: "发送", exact: true})).toBeEnabled();
      await page.getByRole("button", {name: "发送", exact: true}).click();
      await chatRun(other, text);
      await page.getByRole("link", {name: "记忆", exact: true}).click();
    };
    const queue = page.getByRole("region", {name: "提炼队列"});
    const batch = label => queue.locator("article[data-batch]", {hasText: label});
    const systemRuns = async () => (await other.get("/api/runs?filter=system&limit=25")).body.runs.length;
    await send("第一句闲聊");
    await send("第二句闲聊");

    await expect(queue.getByText(/达到阈值后.*不提供绕过阈值的立即提炼/)).toBeVisible();
    await expect(queue.getByRole("button", {name: /立即|生成/})).toHaveCount(0);
    const first = batch("待批准（覆盖人工保护记忆）");
    await expect(first).toHaveCount(1, {timeout: 10000});
    await expect(queue).toContainText("有效未处理 2 / 阈值 2");
    await first.getByRole("button", {name: "查看候选", exact: true}).click();
    await expect(first.getByLabel("候选差异")).toContainText(`update ${id} · 饮食：Zephyr likes coriander（提炼改写）`);
    await expect(first.getByLabel("候选差异")).toContainText("情景摘要：浏览器提炼摘要");
    await server.send("busy");
    await first.getByRole("button", {name: "批准整批", exact: true}).click();
    await expect(queue.getByText("宿主正忙：操作未执行，也不会排队；请稍后手动重试。")).toBeVisible();
    await server.send("idle");
    await expect(first.getByRole("button", {name: "批准整批", exact: true})).toBeVisible();
    await first.getByRole("button", {name: "拒绝整批", exact: true}).click();
    await expect(queue.getByText("已完成：rejected。")).toBeVisible();
    await expect(batch("已拒绝")).toHaveCount(1);
    expect((await other.get(`/api/memory/record?kind=semantic&id=${id}`)).body.record.fact).toBe("Zephyr likes coriander");

    await send("第三句闲聊");
    await send("第四句闲聊");
    const second = batch("待批准（覆盖人工保护记忆）");
    await expect(second).toHaveCount(1, {timeout: 10000});
    const secondId = await second.getAttribute("data-batch");
    await second.getByRole("button", {name: "查看候选", exact: true}).click();
    expect((await other.command({
      operation_id: "target-edit", kind: "semantic", action: "update",
      payload: {id, fact: "Zephyr likes basil"}, expected_version: 1,
    })).status).toBe(200);
    const invalidated = queue.locator(`article[data-batch="${secondId}"]`);
    await expect(invalidated).toContainText("已失效：依赖记录或来源已变化，候选正文已清除");
    await expect(invalidated.getByRole("button")).toHaveCount(0);
    await expect(queue).not.toContainText("提炼改写");

    await server.send("plan invalid");
    await send("第五句闲聊");
    const failed = batch("失败");
    await expect(failed).toHaveCount(1, {timeout: 10000});
    await expect(failed).toContainText("重试：候选仍有效时只重试提交；否则会重新调用主模型并可能产生费用。");
    await server.send("busy");
    await failed.getByRole("button", {name: "重试", exact: true}).click();
    await expect(queue.getByText("宿主正忙：操作未执行，也不会排队；请稍后手动重试。")).toBeVisible();
    await server.send("idle");
    await server.send("plan create");
    await failed.getByRole("button", {name: "重试", exact: true}).click();
    await expect(queue.getByText(/^已受理新的提炼运行（Run .+）：它会再次调用主模型并可能产生费用，结果待确认。$/)).toBeVisible();
    await expect(batch("已提交")).toHaveCount(1, {timeout: 10000});
    const semantic = page.getByRole("tabpanel", {name: "语义记忆"});
    const distilled = semantic.getByLabel("语义记忆列表").locator("article", {hasText: "浏览器提炼事实"});
    await expect(distilled).toContainText(/创建：记忆提炼 · 批次 \S+ · .+ · 未受人工保护/);
    await distilled.getByRole("button", {name: "查看详情", exact: true}).click();
    await semantic.getByRole("button", {name: "查看提炼批次", exact: true}).click();
    await expect(batch("已提交")).toContainText(/来源对话 \[.+, .+\]/);
    await expect(batch("已提交")).toContainText("候选不可用：已完成、失效或依赖已删，不显示旧正文。");

    // A valid candidate whose commit failed is retried without another model call.
    await server.send("fail-commit");
    await send("第六句闲聊");
    const uncommitted = batch("失败");
    await expect(uncommitted).toHaveCount(1, {timeout: 10000});
    const runs = await systemRuns();
    await server.send("heal");
    await uncommitted.getByRole("button", {name: "重试", exact: true}).click();
    await expect(queue.getByText("已完成：succeeded。")).toBeVisible();
    await expect(batch("已提交")).toHaveCount(2);
    expect(await systemRuns()).toBe(runs);

    await server.send("plan update");
    await send("第七句闲聊");
    await send("第八句闲聊");
    const third = batch("待批准（覆盖人工保护记忆）");
    await expect(third).toHaveCount(1, {timeout: 10000});
    await third.getByRole("button", {name: "批准整批", exact: true}).click();
    await expect(queue.getByText("已完成：succeeded。")).toBeVisible();
    await expect(batch("已提交")).toHaveCount(3);
  } finally {
    await server.close();
  }
});

test("A55: an undeliverable memory_patch closes the real stream and reconnect verifies", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({
      operation_id: "seed", kind: "semantic", action: "save",
      payload: {subject: "Zephyr", fact: "Zephyr likes coriander"},
    });
    const streams = [];
    const states = [];
    page.on("request", request => {
      if (request.url().includes("/api/events")) streams.push(request.url());
      if (request.url().endsWith("/api/memory/state")) states.push(request.url());
    });
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await panel.getByRole("button", {name: "查看详情", exact: true}).click();
    const detail = panel.getByLabel("语义记忆详情");
    await expect(detail.getByText("Zephyr likes coriander")).toBeVisible();
    const before = {streams: streams.length, states: states.length};
    const watched = await watchText(page, "coriander");
    await server.send("overflow");
    expect((await other.command({
      operation_id: "delete", kind: "semantic", action: "delete",
      payload: {id: saved.body.result.memory_id}, expected_version: 1,
    })).body.result.status).toBe("deleted");
    await expect(detail.getByText("记录已不存在（可能已被删除）；不显示旧正文。")).toBeVisible({timeout: 10000});
    await expect(panel.getByText("记忆库为空。", {exact: true})).toBeVisible();
    expect(streams.length).toBeGreaterThan(before.streams);
    expect(states.length).toBeGreaterThan(before.states);
    expect(await watched()).toMatchObject({hidden: true, returned: false});
    expect((await other.get("/api/runs?filter=all&limit=25")).body.runs).toEqual([]);
  } finally {
    await server.close();
  }
});

test("R14: a mirror write failure is shown apart from the saved record and retried explicitly", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    await page.goto(server.origin + "/memory");
    const facts = page.getByRole("region", {name: "Markdown 镜像"}).locator('[data-mirror="facts"]');
    await expect(facts).toContainText("已同步并核验。");
    await server.send("mirror-fail on");
    expect((await other.command({
      operation_id: "seed", kind: "semantic", action: "save",
      payload: {subject: "Zephyr", fact: "Zephyr likes coriander"},
    })).body.result.status).toBe("saved");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await expect(panel.getByLabel("语义记忆列表").getByText("Zephyr likes coriander")).toBeVisible();
    await expect(facts).toContainText("同步失败（mirror_sync_failed）：数据库中的记忆不受影响。");
    await facts.getByRole("button", {name: "预览", exact: true}).click();
    await expect(facts.locator("pre")).toHaveText("当前镜像未核验或不可读，不显示旧内容。");
    await facts.getByRole("button", {name: "同步重试", exact: true}).click();
    const mirrors = page.getByRole("region", {name: "Markdown 镜像"});
    await expect(mirrors.getByText("镜像同步失败：数据库中的记忆不受影响，可再次同步重试。")).toBeVisible();
    await server.send("mirror-fail off");
    // The retry commits but its response is lost: query the same operation.
    await page.route("**/api/memory/consolidation/actions", async route => {
      await route.fetch();
      await route.abort("connectionreset");
    });
    await facts.getByRole("button", {name: "同步重试", exact: true}).click();
    await expect(mirrors.getByText(/^结果待确认（network）：无回执不等于失败。/)).toBeVisible();
    await page.unroute("**/api/memory/consolidation/actions");
    await mirrors.getByRole("button", {name: "查询结果", exact: true}).click();
    await expect(mirrors.getByText("镜像操作完成：regenerated。")).toBeVisible();
    await expect(facts).toContainText("已同步并核验。");
    await facts.getByRole("button", {name: "预览", exact: true}).click();
    await expect(facts.locator("pre")).toContainText("Zephyr likes coriander");
  } finally {
    await server.close();
  }
});

test("R10/R14: an external mirror edit keeps cleanup unfinished until regeneration is confirmed", async ({page}) => {
  const {readFile, writeFile} = await import("node:fs/promises");
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({
      operation_id: "seed", kind: "semantic", action: "save",
      payload: {subject: "Zephyr", fact: "Zephyr likes coriander"},
    });
    await expect.poll(async () => readFile(`${server.directory}/memory/facts.md`, "utf8")).toContain("coriander");
    await writeFile(`${server.directory}/memory/facts.md`, "外部编辑 coriander");
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await panel.getByRole("button", {name: "查看详情", exact: true}).click();
    await panel.getByRole("button", {name: "删除", exact: true}).click();
    await panel.getByRole("button", {name: "确认删除", exact: true}).click();
    const receipts = page.getByRole("region", {name: "记忆操作回执"});
    const receipt = receipts.locator("article").first();
    await expect(receipt).toContainText(`记忆已删除（数据库已确认） · ID ${saved.body.result.memory_id}`);
    await expect(receipt).toContainText("记忆已删除，副本清理未完成。");
    await expect(receipt).toContainText("memory/facts.md · failed · cleanup_unconfirmed");

    const mirrors = page.getByRole("region", {name: "Markdown 镜像"});
    const facts = mirrors.locator('[data-mirror="facts"]');
    await expect(facts).toContainText("检测到外部修改：已停止覆盖，等待确认重新生成。");
    await expect(facts.getByRole("button", {name: "同步重试"})).toHaveCount(0);
    await facts.getByRole("button", {name: "预览", exact: true}).click();
    await expect(facts.locator("pre")).toHaveText("当前镜像未核验或不可读，不显示旧内容。");

    await receipt.getByRole("button", {name: "重试清理", exact: true}).click();
    await expect(receipt).toContainText("记忆已删除，副本清理未完成。");
    // The unfinished cleanup is durable; a reload reads the current observation.
    await page.reload();
    await expect(receipt).toContainText("记忆已删除，副本清理未完成。");
    await facts.getByRole("button", {name: "确认重新生成", exact: true}).click();
    await expect(mirrors.getByText("镜像操作完成：regenerated。")).toBeVisible();
    await expect(facts).toContainText("已同步并核验。");
    await expect(receipt).toContainText("遗忘完成：条目、索引与受管副本均已清理并核实。");
    await expect(receipt.getByRole("button", {name: "重试清理"})).toHaveCount(0);
    await facts.getByRole("button", {name: "预览", exact: true}).click();
    await expect(facts.locator("pre")).toContainText("# Facts");
    await expect(page.getByText(/coriander/)).toHaveCount(0);
    expect(await readFile(`${server.directory}/memory/facts.md`, "utf8")).not.toContain("coriander");
  } finally {
    await server.close();
  }
});

test.describe("episodic time", () => {
  test.use({timezoneId: "Asia/Shanghai"});
  test("R11: aware local input, half-open display and boundary search", async ({page}) => {
    const server = await memoryServer();
    try {
      await page.goto(server.origin + "/memory?tab=episodic");
      const panel = page.getByRole("tabpanel", {name: "情景记忆"});
      await expect(panel.getByText("记忆库为空。", {exact: true})).toBeVisible();
      await expect(panel.getByText("时间按本机时区解释并带 offset 提交；终点不含。")).toBeVisible();
      const create = async (summary, start, end) => {
        await panel.getByLabel("新摘要").fill(summary);
        await panel.getByLabel("新发生起点").fill(start);
        if (end) await panel.getByLabel("新发生终点（留空表示瞬时）").fill(end);
        const submitted = end ? /^将提交 \[.+\+08:00, .+\+08:00\)$/ : /^将提交 瞬时 .+\+08:00$/;
        await expect(panel.getByLabel("新增情景记忆").getByText(submitted)).toBeVisible();
        await panel.getByRole("button", {name: "保存", exact: true}).click();
        await expect(panel.getByText(/^已保存（ID .+，版本 1）。$/)).toBeVisible();
      };
      await create("去杭州出差", "2026-09-01T09:00", "2026-09-02T18:00");
      await create("开会", "2026-09-03T10:00");
      const list = panel.getByLabel("情景记忆列表");
      await expect(list).toContainText("[2026-09-01T01:00:00+00:00, 2026-09-02T10:00:00+00:00)");
      await expect(list).toContainText("瞬时 · 2026-09-03T02:00:00+00:00");

      await panel.getByLabel("起点（包含）").fill("2026-09-02T18:00");
      await expect(panel.getByText("将查询 [2026-09-02T18:00:00+08:00, 无上界)：起点包含、终点不含。")).toBeVisible();
      await panel.getByRole("button", {name: "搜索", exact: true}).click();
      await expect(list.locator("article")).toHaveCount(1);
      await expect(list).toContainText("开会");
      await panel.getByLabel("起点（包含）").fill("");
      await panel.getByLabel("终点（不含）").fill("2026-09-03T10:00");
      await panel.getByRole("button", {name: "搜索", exact: true}).click();
      await expect(list.locator("article")).toHaveCount(1);
      await expect(list).toContainText("去杭州出差");

      await panel.getByRole("button", {name: "显示全部", exact: true}).click();
      await list.locator("article", {hasText: "开会"}).getByRole("button", {name: "查看详情"}).click();
      await panel.getByLabel("情景记忆详情").getByRole("button", {name: "编辑", exact: true}).click();
      await expect(panel.getByLabel("发生起点", {exact: true})).toHaveValue("2026-09-03T10:00");
      await panel.getByLabel("发生终点（留空表示瞬时）", {exact: true}).fill("2026-09-03T11:00");
      await panel.getByRole("button", {name: "保存修改", exact: true}).click();
      await expect(panel.getByText("已更新到版本 2。")).toBeVisible();
      await expect(list).toContainText("[2026-09-03T02:00:00+00:00, 2026-09-03T03:00:00+00:00)");
    } finally {
      await server.close();
    }
  });
});

const SEED_OVERSIZED = `
import sqlite3, sys, threading
from pathlib import Path
sys.path.insert(0, "src")
from agent_alfred import schema
from agent_alfred.evals.deterministic.test_memory_consolidation_service import _complete_chat
from agent_alfred.memory.audit import AuditKey
from agent_alfred.memory.commands import MemoryCommandService
from agent_alfred.redact import Redactor
from agent_alfred.runtime.recording import RecordingStore
state = Path(sys.argv[1])
conn = sqlite3.connect(state / "db.sqlite3")
schema.migrate(conn)
memory = MemoryCommandService(
    recording_store=RecordingStore(conn, threading.Lock()),
    audit_key=AuditKey.load_or_create(state / "audit.key", Redactor(())),
    clock=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
)
_complete_chat(memory, conn, "s", "huge", "PRIVATE" * 12000, "ok")
_complete_chat(memory, conn, "s", "small", "next", "ok")
conn.close()
`;

test("R13: an oversized source blocks its Session until the user explicitly skips it", async ({page}) => {
  const {execFileSync} = await import("node:child_process");
  const server = await memoryServer({
    threshold: 2,
    prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", SEED_OVERSIZED, directory]),
  });
  try {
    const other = await api(page.request, server.origin);
    await page.addInitScript(() => sessionStorage.setItem("alfred.session", "s"));
    await page.goto(server.origin + "/inbox");
    await page.getByRole("textbox", {name: "消息"}).fill("触发一次机会");
    await page.getByRole("button", {name: "发送", exact: true}).click();
    await chatRun(other, "触发一次机会");
    await page.getByRole("link", {name: "记忆", exact: true}).click();
    const queue = page.getByRole("region", {name: "提炼队列"});
    await expect(queue).toContainText(/来源超限：Run huge 共 \d+ 字符，上限 64000；该会话提炼已停止。/, {timeout: 10000});
    await expect(queue).not.toContainText("PRIVATE");
    await queue.getByRole("button", {name: "跳过此来源", exact: true}).click();
    await expect(queue.getByText("已完成：skipped。")).toBeVisible();
    await expect(queue.getByRole("button", {name: "跳过此来源"})).toHaveCount(0);
    expect((await other.get("/api/sessions/messages?session_id=s")).body.messages.length).toBeGreaterThanOrEqual(4);
  } finally {
    await server.close();
  }
});

test("R08: a lost response stays unconfirmed until queried or resent with the same id", async ({page}) => {
  const server = await memoryServer();
  try {
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    const receipts = page.getByRole("region", {name: "记忆操作回执"});
    const bodies = [];
    let mode = "commit-then-drop";
    await page.route("**/api/memory/commands", async route => {
      bodies.push(route.request().postDataJSON());
      if (mode === "commit-then-drop") {
        await route.fetch();
        return route.abort("connectionreset");
      }
      if (mode === "drop") return route.abort("connectionreset");
      return route.continue();
    });
    // The page reads a lost response back against the persisted fact by
    // itself; holding that read shows what the user is offered until it lands.
    let answering = false;
    await page.route("**/api/memory/operations?*", route =>
      answering ? route.continue() : route.abort("connectionreset"));
    await panel.getByLabel("新主题").fill("饮食");
    await panel.getByLabel("新事实").fill("committed but unanswered");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await expect(panel.getByText("结果待确认：草稿已保留，可在操作回执中查询或重新发送同一操作。")).toBeVisible();
    await expect(receipts).toContainText("结果待确认（network）：无回执不等于失败");
    await page.reload();
    const pending = receipts.locator("article").first();
    await expect(pending).toContainText("结果待确认（network）");
    answering = true;
    await pending.getByRole("button", {name: "查询结果", exact: true}).click();
    await expect(pending).toContainText(/已保存 · ID .+ · 版本 1 · 提交于/);
    await page.unroute("**/api/memory/operations?*");

    mode = "drop";
    await panel.getByLabel("新主题").fill("饮食");
    await panel.getByLabel("新事实").fill("never arrived");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    const lost = receipts.locator("article").first();
    await expect(lost).toContainText("结果待确认（network）");
    await lost.getByRole("button", {name: "查询结果", exact: true}).click();
    await expect(lost).toContainText("尚未找到提交记录：仍未确认，可重新发送同一操作。");
    mode = "pass";
    await lost.getByRole("button", {name: "重新发送同一操作", exact: true}).click();
    await expect(lost).toContainText(/已保存 · ID .+ · 版本 1/);
    expect(bodies.at(-1)).toEqual(bodies.at(-2));

    // Saving the same unconfirmed draft again is the same user action.
    mode = "drop";
    await panel.getByLabel("新主题").fill("饮食");
    await panel.getByLabel("新事实").fill("saved twice from the form");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await expect(panel.getByText("结果待确认：草稿已保留，可在操作回执中查询或重新发送同一操作。")).toBeVisible();
    mode = "pass";
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await expect(panel.getByText(/^已保存（ID .+，版本 1）。$/)).toBeVisible();
    expect(bodies.at(-1).operation_id).toBe(bodies.at(-2).operation_id);
    const list = panel.getByLabel("语义记忆列表");
    await expect(list.locator("article")).toHaveCount(3);
    await expect(receipts.locator("article", {hasText: "结果待确认"})).toHaveCount(0);

    mode = "drop";
    await list.locator("article", {hasText: "saved twice from the form"})
      .getByRole("button", {name: "查看详情", exact: true}).click();
    const detail = panel.getByLabel("语义记忆详情");
    await detail.getByRole("button", {name: "删除", exact: true}).click();
    await detail.getByRole("button", {name: "确认删除", exact: true}).click();
    await expect(detail.getByText("删除结果待确认：可在操作回执中查询或重新发送同一操作。")).toBeVisible();
    mode = "pass";
    await detail.getByRole("button", {name: "删除", exact: true}).click();
    await detail.getByRole("button", {name: "确认删除", exact: true}).click();
    await expect(detail.getByText("记忆已删除；遗忘进度见操作回执。")).toBeVisible();
    expect(bodies.at(-1).operation_id).toBe(bodies.at(-2).operation_id);
    await expect(receipts.locator("article", {hasText: "结果待确认"})).toHaveCount(0);
  } finally {
    await server.close();
  }
});

for (const committed of [false, true]) {
  test(`R08: reload during an unanswered command preserves its identity (committed=${committed})`, async ({page}) => {
    const server = await memoryServer();
    let release;
    const held = new Promise(resolve => { release = resolve; });
    let received;
    const arrived = new Promise(resolve => { received = resolve; });
    const bodies = [];
    try {
      await page.goto(server.origin + "/memory");
      page.on("request", request => {
        if (request.url().endsWith("/api/memory/commands")) bodies.push(request.postDataJSON());
      });
      await page.route("**/api/memory/commands", async route => {
        if (committed) await route.fetch();
        received();
        await held;
        await route.abort("connectionreset").catch(() => {});
      });
      // Hold the automatic read-back so the restored unconfirmed state, and
      // then the user's own query, are both observable.
      let answering = false;
      await page.route("**/api/memory/operations?*", route =>
        answering ? route.continue() : route.abort("connectionreset"));
      const panel = page.getByRole("tabpanel", {name: "语义记忆"});
      await panel.getByLabel("新主题").fill("恢复");
      await panel.getByLabel("新事实").fill("reload before any command response");
      await panel.getByRole("button", {name: "保存", exact: true}).click();
      await arrived;
      await page.reload();
      release();
      await page.unroute("**/api/memory/commands");
      const receipt = page.getByRole("region", {name: "记忆操作回执"})
        .locator("article", {hasText: bodies[0].operation_id});
      await expect(receipt).toContainText(/结果待确认（(?:interrupted|network)）/);
      answering = true;
      await receipt.getByRole("button", {name: "查询结果", exact: true}).click();
      if (committed) {
        await expect(receipt).toContainText(/已保存 · ID .+ · 版本 1/);
        expect(bodies).toHaveLength(1);
      } else {
        await expect(receipt).toContainText("尚未找到提交记录：仍未确认，可重新发送同一操作。");
        await receipt.getByRole("button", {name: "重新发送同一操作", exact: true}).click();
        await expect(receipt).toContainText(/已保存 · ID .+ · 版本 1/);
        expect(bodies).toHaveLength(2);
        expect(bodies[1]).toEqual(bodies[0]);
      }
      await expect(panel.getByLabel("语义记忆列表").locator("article")).toHaveCount(1);
    } finally {
      release();
      await server.close();
    }
  });
}

test("R08: more than twenty unanswered commands remain recoverable after reload", async ({page}) => {
  // 21 serial failed writes plus reload/query/resend exceed 15s with 600ms
  // transport latency (measured 18.5s). Keep per-assertion timeouts unchanged.
  test.setTimeout(30000);
  const server = await memoryServer();
  const bodies = [];
  try {
    await page.goto(server.origin + "/memory");
    await page.route("**/api/memory/commands", async route => {
      bodies.push(route.request().postDataJSON());
      await route.abort("connectionreset");
    });
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    const receipts = page.getByRole("region", {name: "记忆操作回执"});
    await panel.getByLabel("新主题").fill("恢复");
    for (let i = 0; i < 21; i++) {
      await panel.getByLabel("新事实").fill(`unanswered fact ${i}`);
      await panel.getByRole("button", {name: "保存", exact: true}).click();
      await expect(receipts.locator("article").first()).toContainText("结果待确认（network）");
    }
    await page.reload();
    await expect(receipts.locator("article", {hasText: "结果待确认"})).toHaveCount(21);
    const first = receipts.locator("article", {hasText: bodies[0].operation_id});
    await first.getByRole("button", {name: "查询结果", exact: true}).click();
    await expect(first).toContainText("尚未找到提交记录：仍未确认，可重新发送同一操作。");
    await page.unroute("**/api/memory/commands");
    const resent = page.waitForRequest(request => request.url().endsWith("/api/memory/commands"));
    await first.getByRole("button", {name: "重新发送同一操作", exact: true}).click();
    expect((await resent).postDataJSON()).toEqual(bodies[0]);
    await expect(first).toContainText(/已保存 · ID .+ · 版本 1/);
    await expect(receipts.locator("article", {hasText: "结果待确认"})).toHaveCount(20);
  } finally {
    await server.close();
  }
});

const SEED_LEGACY = `
import sys
from pathlib import Path
sys.path[:0] = ["tests/browser", "src"]
from server import seed_v2
seed_v2(Path(sys.argv[1]) / "db.sqlite3")
`;

test("R09: unknown legacy history needs an explicit scope before forgetting completes", async ({page}) => {
  const {execFileSync} = await import("node:child_process");
  const server = await memoryServer({
    prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", SEED_LEGACY, directory]),
  });
  try {
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await panel.getByLabel("新主题").fill("饮食");
    await panel.getByLabel("新事实").fill("legacy scoped fact");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await panel.getByRole("button", {name: "查看详情", exact: true}).click();
    await panel.getByRole("button", {name: "删除", exact: true}).click();
    await panel.getByRole("button", {name: "确认删除", exact: true}).click();
    const receipt = page.getByRole("region", {name: "记忆操作回执"}).locator("article").first();
    await expect(receipt).toContainText("记忆已删除（数据库已确认）");
    await expect(receipt).toContainText("来源关联不完整：确认隔离范围之前遗忘尚未完成。");
    const scopes = receipt.getByRole("group", {name: "待确认的历史范围（服务端快照）"});
    const scope = scopes.getByLabel(/^legacy legacy \/会话\? · 55 个历史组 · 时间未知 至 时间未知 · 修订 1/);
    await expect(scope).toBeVisible();
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    await expect(receipt).toContainText("来源关联不完整");
    await scope.check();
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    await expect(receipt).toContainText("遗忘完成：条目、索引与受管副本均已清理并核实。");
    await expect(receipt.getByRole("group")).toHaveCount(0);
    await expect(receipt).toContainText("原始会话与删除前的追踪仍可人工查看");
  } finally {
    await server.close();
  }
});

for (const action of ["save", "update"]) {
  for (const responseState of ["lost", "inflight"]) {
    test(`G2 R10: forgotten ${action} clears recovery bodies while response is ${responseState}`, async ({page}) => {
      const server = await memoryServer();
      let release;
      const held = new Promise(resolve => { release = resolve; });
      let arrived;
      const submitted = new Promise(resolve => { arrived = resolve; });
      const secret = `forgotten-${action}-${responseState}`;
      let result;
      try {
        const other = await api(page.request, server.origin);
        if (action === "update") await other.command({operation_id: "seed", kind: "semantic", action: "save", payload: {subject: "private", fact: "before"}});
        await page.goto(server.origin + "/memory");
        const panel = page.getByRole("tabpanel", {name: "语义记忆"});
        const draft = action === "save" ? panel.getByLabel("新事实") : panel.getByLabel("事实", {exact: true});
        if (action === "save") await panel.getByLabel("新主题").fill("private");
        else {
          await panel.getByRole("button", {name: "查看详情", exact: true}).click();
          await panel.getByRole("button", {name: "编辑", exact: true}).click();
        }
        await draft.fill(secret);
        await page.route("**/api/memory/commands", async route => {
          const response = await route.fetch();
          result = (await response.json()).result;
          arrived();
          if (responseState === "inflight") await held;
          await route.abort("connectionreset").catch(() => {});
        });
        await panel.getByRole("button", {name: action === "save" ? "保存" : "保存修改", exact: true}).click();
        await submitted;
        const deleted = await other.command({operation_id: "forget", kind: "semantic", action: "delete", payload: {id: result.memory_id}, expected_version: result.record_version});
        expect(deleted.body.forgetting.state).toBe("complete");
        await expect(panel.getByLabel("语义记忆列表").locator("article")).toHaveCount(0);
        await expect(draft).toHaveValue("");
        await expect.poll(() => page.evaluate(value => (sessionStorage.getItem("alfred.memory.operations") || "").includes(value), secret)).toBe(false);
        release();
        await page.reload();
        await expect.poll(() => page.evaluate(value => (sessionStorage.getItem("alfred.memory.operations") || "").includes(value), secret)).toBe(false);
        await expect(panel.getByLabel("语义记忆列表").locator("article")).toHaveCount(0);
        expect((await other.get(`/api/memory/record?kind=semantic&id=${result.memory_id}`)).status).toBe(404);
      } finally {
        release();
        await server.close();
      }
    });
  }
}

for (const action of ["save", "update", "delete"]) {
  for (const fault of ["truncated-json", "interrupted-body", "missing-field"]) {
    test(`G2 R08: ${action} keeps identity after ${fault} under success headers`, async ({page}) => {
      const server = await memoryServer();
      try {
        const other = await api(page.request, server.origin);
        if (action !== "save") await other.command({operation_id: "seed", kind: "semantic", action: "save", payload: {subject: "receipt", fact: "before"}});
        // Fault only at the browser's HTTP boundary: the real request commits,
        // then a success Response carries an incomplete body to its consumer.
        await page.addInitScript(({action, fault}) => {
          const original = window.fetch.bind(window);
          window.fetch = async (...args) => {
            const response = await original(...args);
            if (!String(args[0]).endsWith("/api/memory/commands")) return response;
            const body = await response.json();
            if (fault === "interrupted-body") return new Response(new ReadableStream({start(controller) {
              controller.enqueue(new TextEncoder().encode('{"result":'));
              controller.error(new TypeError("response body connection lost"));
            }}), {status: 200});
            if (fault === "missing-field") {
              delete body.result[action === "delete" ? "committed_at" : "record_version"];
              return new Response(JSON.stringify(body), {status: 200});
            }
            return new Response('{"result":', {status: 200});
          };
        }, {action, fault});
        await page.route("**/api/memory/operations?*", route => route.abort("connectionreset"));
        await page.goto(server.origin + "/memory");
        const panel = page.getByRole("tabpanel", {name: "语义记忆"});
        const receipts = page.getByRole("region", {name: "记忆操作回执"});
        if (action === "save") {
          await panel.getByLabel("新主题").fill("receipt");
          await panel.getByLabel("新事实").fill("keep my draft");
        } else {
          await panel.getByRole("button", {name: "查看详情", exact: true}).click();
          await panel.getByRole("button", {name: action === "update" ? "编辑" : "删除", exact: true}).click();
          if (action === "update") await panel.getByLabel("事实", {exact: true}).fill("keep my draft");
        }
        const sent = page.waitForRequest(request => request.url().endsWith("/api/memory/commands"));
        await panel.getByRole("button", {name: {save: "保存", update: "保存修改", delete: "确认删除"}[action], exact: true}).click();
        const request = (await sent).postDataJSON();
        const receipt = receipts.locator("article", {hasText: request.operation_id});
        await expect(receipt).toContainText("结果待确认（malformed_response）");
        const stored = await page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.operations")));
        expect(stored.find(entry => entry.operation_id === request.operation_id).request).toEqual(request);
        if (action !== "delete") await expect(panel.getByLabel(action === "save" ? "新事实" : "事实", {exact: true})).toHaveValue("keep my draft");
        await expect(panel).not.toContainText("undefined");
        await page.unroute("**/api/memory/operations?*");
        await receipt.getByRole("button", {name: "查询结果", exact: true}).click();
        await expect(receipt).toContainText("提交于");
        const confirmed = await page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.operations")));
        expect(confirmed.find(entry => entry.operation_id === request.operation_id).request).toBeUndefined();
        expect((await other.get(`/api/memory/operations?operation_id=${request.operation_id}`)).body.result.action).toBe(action);
      } finally {
        await server.close();
      }
    });
  }
}

test("G2 D09: all unanswered mirror actions survive navigation and reload", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    await server.send("mirror-fail on");
    await other.command({operation_id: "s", kind: "semantic", action: "save", payload: {subject: "mirror", fact: "fact"}});
    await other.command({operation_id: "e", kind: "episodic", action: "save", payload: {summary: "episode", occurred_at: "2026-09-12T00:00:00+00:00", occurred_until: null}});
    await page.goto(server.origin + "/memory");
    const mirrors = page.getByRole("region", {name: "Markdown 镜像"});
    const bodies = [];
    let drop = true;
    await page.route("**/api/memory/consolidation/actions", async route => {
      bodies.push(route.request().postDataJSON());
      if (drop) await route.abort("connectionreset");
      else await route.continue();
    });
    for (const name of ["facts", "episodes"]) {
      await mirrors.locator(`[data-mirror="${name}"]`).getByRole("button", {name: "同步重试", exact: true}).click();
      await expect.poll(() => bodies.length).toBe(name === "facts" ? 1 : 2);
    }
    await page.getByRole("link", {name: "收件箱", exact: true}).click();
    await page.getByRole("link", {name: "记忆", exact: true}).click();
    await expect(mirrors.getByRole("button", {name: "查询结果", exact: true})).toHaveCount(2);
    await page.reload();
    await expect(mirrors.getByRole("button", {name: "查询结果", exact: true})).toHaveCount(2);
    expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.actions")).map(entry => entry.action))).toEqual(bodies);
    await server.send("mirror-fail off");
    drop = false;
    await mirrors.getByRole("button", {name: "重新发送同一操作", exact: true}).first().click();
    await expect(mirrors.getByRole("button", {name: "查询结果", exact: true})).toHaveCount(1);
    await mirrors.getByRole("button", {name: "重新发送同一操作", exact: true}).click();
    await expect(mirrors.getByRole("button", {name: "查询结果", exact: true})).toHaveCount(0);
    expect(bodies.slice(2)).toEqual(bodies.slice(0, 2));
    expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.actions")))).toEqual([]);
  } finally {
    await server.close();
  }
});

test("G2 D09: a late old-page query cannot erase a newer action identity", async ({page}) => {
  const server = await memoryServer();
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let captured;
  const capturedQuery = new Promise(resolve => { captured = resolve; });
  let oldRequest;
  try {
    const other = await api(page.request, server.origin);
    await server.send("mirror-fail on");
    await other.command({operation_id: "s", kind: "semantic", action: "save", payload: {subject: "mirror", fact: "fact"}});
    await page.goto(server.origin + "/memory");
    const mirrors = page.getByRole("region", {name: "Markdown 镜像"});
    const retry = mirrors.locator('[data-mirror="facts"]').getByRole("button", {name: "同步重试", exact: true});
    const bodies = [];
    await page.route("**/api/memory/consolidation/actions", async route => {
      bodies.push(route.request().postDataJSON());
      if (bodies.length === 1) await route.fetch();
      await route.abort("connectionreset");
    });
    await retry.click();
    await expect(mirrors.getByRole("button", {name: "查询结果", exact: true})).toHaveCount(1);
    let reads = 0;
    await page.route("**/api/memory/mirrors?operation_id=*", async route => {
      if (++reads !== 1) return route.continue();
      const response = await route.fetch();
      oldRequest = route.request();
      captured();
      await held;
      await route.fulfill({response});
    });
    await mirrors.getByRole("button", {name: "查询结果", exact: true}).click();
    await capturedQuery;
    await page.getByRole("link", {name: "收件箱", exact: true}).click();
    await page.getByRole("link", {name: "记忆", exact: true}).click();
    await expect(mirrors.getByRole("button", {name: "查询结果", exact: true})).toHaveCount(0);
    await retry.click();
    await expect(mirrors.getByRole("button", {name: "查询结果", exact: true})).toHaveCount(1);
    expect(bodies[1].operation_id).not.toBe(bodies[0].operation_id);
    const finished = page.waitForEvent("requestfinished", request => request === oldRequest);
    release();
    await finished;
    // The completed HTTP read and a browser task boundary allow its consumer
    // to settle before rebuilding from the persisted recovery state.
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
    await page.reload();
    await expect(mirrors.getByRole("button", {name: "查询结果", exact: true})).toHaveCount(1);
    expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.actions")).map(entry => entry.action.operation_id))).toEqual([bodies[1].operation_id]);
  } finally {
    release();
    await server.close();
  }
});

for (const surface of ["mirror", "queue"]) {
  for (const fault of ["truncated-json", "missing-status"]) {
    test(`G2 D09: ${surface} ${fault} keeps a queryable action after reload`, async ({page}) => {
      const server = await memoryServer({threshold: 1});
      try {
        const other = await api(page.request, server.origin);
        if (surface === "mirror") await server.send("mirror-fail on");
        await other.command({operation_id: "seed", kind: "semantic", action: "save", payload: {subject: "protected", fact: "unchanged"}});
        if (surface === "queue") {
          await server.send("plan update");
          await page.goto(server.origin + "/inbox");
          await page.getByRole("button", {name: "新建会话", exact: true}).click();
          await page.getByRole("textbox", {name: "消息"}).fill("trigger candidate");
          await page.getByRole("button", {name: "发送", exact: true}).click();
          await chatRun(other, "trigger candidate");
          await page.getByRole("link", {name: "记忆", exact: true}).click();
        } else await page.goto(server.origin + "/memory");
        const panel = page.getByRole("region", {name: surface === "mirror" ? "Markdown 镜像" : "提炼队列"});
        const button = surface === "mirror"
          ? panel.locator('[data-mirror="facts"]').getByRole("button", {name: "同步重试", exact: true})
          : panel.getByRole("button", {name: "拒绝整批", exact: true});
        await expect(button).toBeVisible();
        if (surface === "mirror") await server.send("mirror-fail off");
        const readPath = surface === "mirror" ? "mirrors" : "consolidation";
        await page.route(`**/api/memory/${readPath}?operation_id=*`, route => route.abort("connectionreset"));
        let sent;
        await page.route("**/api/memory/consolidation/actions", async route => {
          sent = route.request().postDataJSON();
          const response = await route.fetch();
          const body = await response.json();
          delete body.result.status;
          await route.fulfill({status: 200, contentType: "application/json", body: fault === "truncated-json" ? '{"result":' : JSON.stringify(body)});
        });
        await button.click();
        await expect(panel.getByText("结果待确认（malformed_response）：无回执不等于失败。", {exact: true})).toBeVisible();
        await page.reload();
        await expect(panel.getByRole("button", {name: "查询结果", exact: true})).toHaveCount(1);
        expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.actions"))[0].action)).toEqual(sent);
        await page.unroute(`**/api/memory/${readPath}?operation_id=*`);
        await page.route(`**/api/memory/${readPath}?operation_id=*`, route =>
          route.fulfill({status: 200, json: {schema_version: 1, operation_id: sent.operation_id, result: {}}}));
        await panel.getByRole("button", {name: "查询结果", exact: true}).click();
        await expect(panel).toContainText("查询失败：仍未确认。");
        await expect(panel.getByRole("button", {name: "查询结果", exact: true})).toHaveCount(1);
        await page.unroute(`**/api/memory/${readPath}?operation_id=*`);
        await panel.getByRole("button", {name: "查询结果", exact: true}).click();
        await expect(panel).toContainText(surface === "mirror" ? "镜像操作完成：regenerated。" : "已完成：rejected。");
        expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.actions")))).toEqual([]);
      } finally {
        await server.close();
      }
    });
  }
}

test("G2 D09: a queried durable mirror failure is a definite failure, not undefined success", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    await server.send("mirror-fail on");
    await other.command({operation_id: "s", kind: "semantic", action: "save", payload: {subject: "mirror", fact: "fact"}});
    await page.goto(server.origin + "/memory");
    const mirrors = page.getByRole("region", {name: "Markdown 镜像"});
    await page.route("**/api/memory/consolidation/actions", async route => { await route.fetch(); await route.abort("connectionreset"); });
    await mirrors.locator('[data-mirror="facts"]').getByRole("button", {name: "同步重试", exact: true}).click();
    await expect(mirrors).toContainText("结果待确认（network）");
    await mirrors.getByRole("button", {name: "查询结果", exact: true}).click();
    await expect(mirrors).toContainText("镜像同步失败：数据库中的记忆不受影响，可再次同步重试。");
    await expect(mirrors).not.toContainText("undefined");
    expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.actions")))).toEqual([]);
  } finally {
    await server.close();
  }
});

test("G2 R10: a late missing-operation read cannot mask later commit and forgetting", async ({page}) => {
  const server = await memoryServer();
  let send, answer, queried, committed;
  const sendGate = new Promise(resolve => { send = resolve; });
  const answerGate = new Promise(resolve => { answer = resolve; });
  const queryReady = new Promise(resolve => { queried = resolve; });
  const commitReady = new Promise(resolve => { committed = resolve; });
  let result;
  try {
    const other = await api(page.request, server.origin);
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await page.route("**/api/memory/commands", async route => {
      await sendGate;
      result = (await (await route.fetch()).json()).result;
      committed();
      await route.abort("connectionreset");
    });
    let first = true;
    await page.route("**/api/memory/operations?*", async route => {
      if (!first) return route.continue();
      first = false;
      const response = await route.fetch();
      expect(response.status()).toBe(404);
      queried();
      await answerGate;
      await route.fulfill({response});
    });
    await panel.getByLabel("新主题").fill("private");
    await panel.getByLabel("新事实").fill("must disappear after forgetting");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    // An unrelated real mutation asks for this still-unsent operation's state.
    await other.command({operation_id: "unrelated", kind: "semantic", action: "save", payload: {subject: "other", fact: "keep"}});
    await queryReady;
    send();
    await commitReady;
    const deleted = await other.command({operation_id: "delete", kind: "semantic", action: "delete", payload: {id: result.memory_id}, expected_version: result.record_version});
    expect(deleted.body.forgetting.state).toBe("complete");
    await expect(panel.getByLabel("语义记忆列表").locator("article")).toHaveCount(1);
    answer();
    await expect(panel.getByLabel("新事实")).toHaveValue("");
    await expect.poll(() => page.evaluate(() => (sessionStorage.getItem("alfred.memory.operations") || "").includes("must disappear after forgetting"))).toBe(false);
  } finally {
    send(); answer();
    await server.close();
  }
});

for (const action of ["save", "update"]) {
  test(`G2 R10: ${action} read failures preserve drafts until reconnect verifies forgetting`, async ({page}) => {
    const server = await memoryServer();
    try {
      const other = await api(page.request, server.origin);
      if (action === "update") await other.command({operation_id: "seed", kind: "semantic", action: "save", payload: {subject: "private", fact: "before"}});
      const stream = await controlledStream(page, server.origin);
      await page.goto(server.origin + "/memory");
      await stream.connect();
      const panel = page.getByRole("tabpanel", {name: "语义记忆"});
      const draft = panel.getByLabel(action === "save" ? "新事实" : "事实", {exact: true});
      if (action === "save") await panel.getByLabel("新主题").fill("private");
      else {
        await panel.getByRole("button", {name: "查看详情", exact: true}).click();
        await panel.getByRole("button", {name: "编辑", exact: true}).click();
      }
      await draft.fill("offline pending body");
      let result;
      await page.route("**/api/memory/operations?*", route => route.fulfill({status: 503, json: {error: {code: "storage_read_failed"}}}));
      await page.route("**/api/memory/commands", async route => {
        result = (await (await route.fetch()).json()).result;
        await route.abort("connectionreset");
      });
      await panel.getByRole("button", {name: action === "save" ? "保存" : "保存修改", exact: true}).click();
      const receipts = page.getByRole("region", {name: "记忆操作回执"});
      await expect(receipts).toContainText("查询失败（storage_read_failed）：仍未确认。");
      await expect(draft).toHaveValue("offline pending body");
      expect(await page.evaluate(() => sessionStorage.getItem("alfred.memory.operations"))).toContain("offline pending body");
      await stream.emit("error", null);
      await expect(draft).toBeHidden();
      expect((await other.command({operation_id: "delete", kind: "semantic", action: "delete", payload: {id: result.memory_id}, expected_version: result.record_version})).body.forgetting.state).toBe("complete");
      await page.unroute("**/api/memory/operations?*");
      await stream.connect();
      await expect(draft).toHaveValue("");
      await expect.poll(() => page.evaluate(() => (sessionStorage.getItem("alfred.memory.operations") || "").includes("offline pending body"))).toBe(false);
    } finally {
      await server.close();
    }
  });
}

for (const surface of ["mirror", "queue"]) {
  for (const committed of [false, true]) {
    test(`G2 D09: ${surface} sending reload recovers the original operation (committed=${committed})`, async ({page}) => {
      const server = await memoryServer({threshold: 1});
      let release, arrived;
      const held = new Promise(resolve => { release = resolve; });
      const sent = new Promise(resolve => { arrived = resolve; });
      const bodies = [];
      try {
        const other = await api(page.request, server.origin);
        if (surface === "mirror") await server.send("mirror-fail on");
        await other.command({operation_id: "seed", kind: "semantic", action: "save", payload: {subject: "protected", fact: "unchanged"}});
        if (surface === "queue") {
          await server.send("plan update");
          await page.goto(server.origin + "/inbox");
          await page.getByRole("button", {name: "新建会话", exact: true}).click();
          await page.getByRole("textbox", {name: "消息"}).fill("reload candidate");
          await page.getByRole("button", {name: "发送", exact: true}).click();
          await chatRun(other, "reload candidate");
          await page.getByRole("link", {name: "记忆", exact: true}).click();
        } else await page.goto(server.origin + "/memory");
        const panel = page.getByRole("region", {name: surface === "mirror" ? "Markdown 镜像" : "提炼队列"});
        const button = surface === "mirror" ? panel.locator('[data-mirror="facts"]').getByRole("button", {name: "同步重试", exact: true}) : panel.getByRole("button", {name: "拒绝整批", exact: true});
        await expect(button).toBeVisible();
        if (surface === "mirror") await server.send("mirror-fail off");
        await page.route("**/api/memory/consolidation/actions", async route => {
          bodies.push(route.request().postDataJSON());
          if (committed) await route.fetch();
          arrived();
          await held;
          await route.abort("connectionreset").catch(() => {});
        });
        await button.click();
        await sent;
        await page.reload();
        release();
        await page.unroute("**/api/memory/consolidation/actions");
        if (!committed) {
          await expect(panel).toContainText("尚未找到提交记录：仍未确认，可重新发送同一操作。");
          const resent = page.waitForRequest(request => request.url().endsWith("/api/memory/consolidation/actions"));
          await panel.getByRole("button", {name: "重新发送同一操作", exact: true}).click();
          expect((await resent).postDataJSON()).toEqual(bodies[0]);
        }
        await expect(panel).toContainText(surface === "mirror" ? "镜像操作完成：regenerated。" : "已完成：rejected。");
        expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.actions")))).toEqual([]);
      } finally {
        release();
        await server.close();
      }
    });
  }
}

test("G2 D09: restored confirmation never authorizes a newer external file", async ({page}) => {
  const {readFile, writeFile} = await import("node:fs/promises");
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    await other.command({operation_id: "seed", kind: "semantic", action: "save", payload: {subject: "mirror", fact: "fact"}});
    const path = `${server.directory}/memory/facts.md`;
    await writeFile(path, "external version one");
    await page.goto(server.origin + "/memory");
    const mirrors = page.getByRole("region", {name: "Markdown 镜像"});
    const bodies = [];
    await page.route("**/api/memory/consolidation/actions", async route => {
      bodies.push(route.request().postDataJSON());
      await route.abort("connectionreset");
    });
    await mirrors.locator('[data-mirror="facts"]').getByRole("button", {name: "确认重新生成", exact: true}).click();
    await expect(mirrors).toContainText("结果待确认（network）");
    await writeFile(path, "external version two must survive");
    await page.reload();
    await expect(mirrors).toContainText("尚未找到提交记录：仍未确认，可重新发送同一操作。");
    expect(bodies).toHaveLength(1);
    const stored = await page.evaluate(() => sessionStorage.getItem("alfred.memory.actions"));
    expect(stored).not.toContain("external version");
    await page.unroute("**/api/memory/consolidation/actions");
    const sent = page.waitForRequest(request => request.url().endsWith("/api/memory/consolidation/actions"));
    await mirrors.getByRole("button", {name: "重新发送同一操作", exact: true}).click();
    expect((await sent).postDataJSON()).toEqual(bodies[0]);
    await expect(mirrors).toContainText("镜像文件又发生了变化：旧确认不能授权新的文件版本。");
    expect(await readFile(path, "utf8")).toBe("external version two must survive");
  } finally {
    await server.close();
  }
});

for (const action of ["save", "update"]) {
  test(`G2 v2: ${action} recovery clears forgotten bodies while away and after reload`, async ({page}) => {
    const server = await memoryServer();
    try {
      const other = await api(page.request, server.origin);
      if (action === "update") await other.command({operation_id: "seed-away", kind: "semantic", action: "save", payload: {subject: "away", fact: "before"}});
      await page.goto(server.origin + "/memory");
      const panel = page.getByRole("tabpanel", {name: "语义记忆"});
      await page.route("**/api/memory/operations?*", r => r.fulfill({status: 503, json: {error: {code: "storage_read_failed"}}}));
      await page.route("**/api/memory/commands", async r => {await r.fetch(); await r.abort("failed");});
      if (action === "save") {
        await panel.getByLabel("新主题").fill("away");
        await panel.getByLabel("新事实").fill("AWAY_PRIVATE_BODY");
        await panel.getByRole("button", {name: "保存", exact: true}).click();
      } else {
        await panel.getByRole("button", {name: "查看详情", exact: true}).click();
        await panel.getByRole("button", {name: "编辑", exact: true}).click();
        await panel.getByLabel("事实", {exact: true}).fill("AWAY_PRIVATE_BODY");
        await panel.getByRole("button", {name: "保存修改", exact: true}).click();
      }
      await expect(page.getByRole("region", {name: "记忆操作回执"})).toContainText("仍未确认");
      const record = (await other.get("/api/memory/records?kind=semantic")).body.records[0];
      await page.locator('nav a[href="/runs"]').click();
      if (action === "update") await page.reload();
      const deletion = await other.command({operation_id: "away-delete", kind: "semantic", action: "delete", payload: {id: record.id}, expected_version: record.record_version});
      expect(deletion.body.forgetting.state).toBe("complete");
      // Read failure is not absence, including outside the Memory page.
      expect(await page.evaluate(() => sessionStorage.getItem("alfred.memory.operations"))).toContain("AWAY_PRIVATE_BODY");
      await page.unroute("**/api/memory/operations?*");
      // A subsequent real invalidation retries recovery without visiting Memory.
      await other.command({operation_id: "away-wakeup", kind: "semantic", action: "save", payload: {subject: "other", fact: "unrelated"}});
      await expect.poll(() => page.evaluate(() => (sessionStorage.getItem("alfred.memory.operations") || "").includes("AWAY_PRIVATE_BODY"))).toBe(false);
      await page.reload();
      expect(await page.evaluate(() => sessionStorage.getItem("alfred.memory.operations"))).not.toContain("AWAY_PRIVATE_BODY");
      await page.getByRole("link", {name: "记忆", exact: true}).click();
      await expect(panel.getByLabel("新事实")).toHaveValue("");
      await expect(panel).not.toContainText("AWAY_PRIVATE_BODY");
    } finally { await server.close(); }
  });
}

const SEED_OLD_QUEUE = SEED_OVERSIZED.replace("conn.close()", `
from agent_alfred.evals.deterministic.test_memory_consolidation_service import CONTEXT, MODEL, PLAN, T0
from agent_alfred.memory.consolidation import ConsolidationLimits
memory.consolidation._limits = ConsolidationLimits(source_threshold=2)
assert memory.consolidation.submit("s", PLAN, context=CONTEXT, operation_id="blocked", model=MODEL)["status"] == "source_too_large"
for i in range(50):
    schema.insert_session(conn, session_id=f"empty-{i}", created_at=T0)
conn.commit()
conn.close()
`);

test("G2 v2: an older blocked Session remains reachable and skippable", async ({page}) => {
  const {execFileSync} = await import("node:child_process");
  const server = await memoryServer({threshold: 2, prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", SEED_OLD_QUEUE, directory])});
  try {
    const other = await api(page.request, server.origin);
    const first = (await other.get("/api/memory/consolidation?limit=50&offset=0")).body;
    expect(first.sessions.some(s => s.session_id === "s")).toBe(false);
    expect(first.sessions_has_more).toBe(true);
    await page.goto(server.origin + "/memory");
    const queue = page.getByRole("region", {name: "提炼队列"});
    await queue.getByRole("button", {name: "下一页", exact: true}).click();
    await expect(queue).toContainText("来源超限：Run huge");
    await queue.getByRole("button", {name: "跳过此来源", exact: true}).click();
    await expect(queue).toContainText("已完成：skipped。");
    await expect(queue.getByRole("button", {name: "跳过此来源"})).toHaveCount(0);
    expect((await other.get("/api/memory/consolidation?session_id=s")).body.sessions[0].block).toBeNull();
    expect((await other.get("/api/sessions/messages?session_id=s")).body.messages.length).toBe(4);
  } finally { await server.close(); }
});

const SEED_OLD_BATCHES = SEED_OVERSIZED.replace('conn.close()', `
import json
from agent_alfred.evals.deterministic.test_memory_consolidation_service import CONTEXT, MODEL
from agent_alfred.memory.consolidation import ConsolidationLimits
memory.consolidation._limits = ConsolidationLimits(source_threshold=2)
seeded = memory.execute({"operation_id":"protected", "kind":"semantic", "action":"save", "payload":{"subject":"food", "fact":"likes basil"}}, CONTEXT)
for i in range(52):
    session = f"batch-session-{i}"
    _complete_chat(memory, conn, session, f"source-{i}-a", "I like basil and coriander", "ok")
    _complete_chat(memory, conn, session, f"source-{i}-b", "coriander instead of basil", "ok")
    plan = json.dumps({"semantic":[{"action":"update", "id":seeded["memory_id"], "subject":"food", "fact":"OLDER_CANDIDATE_BODY"}], "episode_summary":"Diet changed."}) if i == 0 else "invalid JSON"
    result = memory.consolidation.submit(session, plan, context=CONTEXT, operation_id=f"batch-op-{i}", model=MODEL)
    assert result["status"] == ("awaiting_approval" if i == 0 else "failed"), result
conn.close()
`);

for (const action of ["approve", "reject", "retry"]) {
  test(`G2 v2: older batches support ${action} through bounded queue pages`, async ({page}) => {
    const {execFileSync} = await import("node:child_process");
    const server = await memoryServer({threshold: 2, prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", SEED_OLD_BATCHES, directory])});
    try {
      const other = await api(page.request, server.origin);
      const old = (await other.get("/api/memory/consolidation?session_id=batch-session-" + (action === "retry" ? 1 : 0))).body.batches[0];
      await page.goto(server.origin + "/memory");
      const queue = page.getByRole("region", {name: "提炼队列"});
      await expect(queue.locator(`[data-batch="${old.batch_id}"]`)).toHaveCount(0);
      await queue.getByRole("button", {name: "下一页", exact: true}).click();
      const row = queue.locator(`[data-batch="${old.batch_id}"]`);
      await row.getByRole("button", {name: {approve: "批准整批", reject: "拒绝整批", retry: "重试"}[action], exact: true}).click();
      await expect.poll(async () => (await other.get("/api/memory/consolidation?batch_id=" + old.batch_id)).body.batch.status).toBe(action === "reject" ? "rejected" : "succeeded");
      if (action === "approve") expect((await other.get("/api/memory/records?kind=semantic")).body.records[0].fact).toBe("OLDER_CANDIDATE_BODY");
      if (action === "reject") expect((await other.get("/api/memory/records?kind=semantic")).body.records[0].fact).toBe("likes basil");
    } finally { await server.close(); }
  });
}

test("G2 v2: queue page and filter changes reject late candidate reads", async ({page}) => {
  const {execFileSync} = await import("node:child_process");
  const server = await memoryServer({threshold: 2, prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", SEED_OLD_BATCHES, directory])});
  try {
    await page.goto(server.origin + "/memory");
    const queue = page.getByRole("region", {name: "提炼队列"});
    await queue.getByRole("button", {name: "下一页", exact: true}).click();
    const row = queue.locator("article", {hasText: "待批准（覆盖人工保护记忆）"});
    let release;
    const held = new Promise(resolve => {release = resolve;});
    let captured = 0;
    let delivered = 0;
    await page.route("**/api/memory/consolidation?batch_id=*", async route => {
      const response = await route.fetch(); captured++;
      await held; await route.fulfill({response}); delivered++;
    });
    await row.getByRole("button", {name: "查看候选", exact: true}).click();
    await expect.poll(() => captured).toBe(1);
    await queue.getByRole("button", {name: "上一页", exact: true}).click();
    await expect(queue).toContainText("第 1 页");
    await queue.getByLabel("按会话 ID 筛选队列").fill("batch-session-0");
    await queue.getByRole("button", {name: "筛选队列", exact: true}).click();
    await row.getByRole("button", {name: "查看候选", exact: true}).click();
    await expect.poll(() => captured).toBe(2);
    await queue.getByLabel("按会话 ID 筛选队列").fill("empty-session");
    await queue.getByRole("button", {name: "筛选队列", exact: true}).click();
    release();
    await expect.poll(() => delivered).toBe(2);
    await expect(queue).toContainText("暂无提炼批次。");
    await expect(queue).not.toContainText("OLDER_CANDIDATE_BODY");
    await expect(queue.locator("[data-batch]")).toHaveCount(0);
    await page.unroute("**/api/memory/consolidation?batch_id=*");
    await queue.getByLabel("按会话 ID 筛选队列").fill("batch-session-0");
    await queue.getByRole("button", {name: "筛选队列", exact: true}).click();
    await row.getByRole("button", {name: "查看候选", exact: true}).click();
    await expect(row.getByLabel("候选差异")).toContainText("OLDER_CANDIDATE_BODY");
    const other = await api(page.request, server.origin);
    const record = (await other.get("/api/memory/records?kind=semantic")).body.records[0];
    await other.command({operation_id: "invalidate-page-candidate", kind: "semantic", action: "delete", payload: {id: record.id}, expected_version: record.record_version});
    await expect(queue).not.toContainText("OLDER_CANDIDATE_BODY");
    await expect(queue).toContainText("候选不可用");
  } finally { await server.close(); }
});

test("G2 v2: an uncommitted update loses its reusable body when its record is forgotten away", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({operation_id: "absent-update-seed", kind: "semantic", action: "save", payload: {subject: "away", fact: "before"}});
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await panel.getByRole("button", {name: "查看详情", exact: true}).click();
    await panel.getByRole("button", {name: "编辑", exact: true}).click();
    await panel.getByLabel("事实", {exact: true}).fill("UNCOMMITTED_PRIVATE_BODY");
    await page.route("**/api/memory/commands", r => r.abort("failed"));
    await panel.getByRole("button", {name: "保存修改", exact: true}).click();
    await expect(page.getByRole("region", {name: "记忆操作回执"})).toContainText("尚未找到提交记录");
    await page.locator('nav a[href="/runs"]').click();
    const deletion = await other.command({operation_id: "absent-update-delete", kind: "semantic", action: "delete", payload: {id: saved.body.result.memory_id}, expected_version: 1});
    expect(deletion.body.forgetting.state).toBe("complete");
    await expect.poll(() => page.evaluate(() => (sessionStorage.getItem("alfred.memory.operations") || "").includes("UNCOMMITTED_PRIVATE_BODY"))).toBe(false);
    const entries = await page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.operations")));
    expect(entries[0].state).toBe("pending");
    expect(entries[0].request).toBeUndefined();
    await page.getByRole("link", {name: "记忆", exact: true}).click();
    await expect(page.getByRole("region", {name: "记忆操作回执"}).getByText("该记录已删除：可复用正文已清除，仅保留同 ID 查询。")).toBeVisible();
  } finally { await server.close(); }
});

test("G2 v3: queue filters preserve distinct historical Session IDs verbatim", async ({page}) => {
  const {execFileSync} = await import("node:child_process");
  const seed = SEED_OVERSIZED.replace("conn.close()", `
_complete_chat(memory, conn, " s ", "padded-source", "padded session", "ok")
_complete_chat(memory, conn, " ", "whitespace-source", "whitespace session", "ok")
conn.close()
`);
  const server = await memoryServer({threshold: 2, prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", seed, directory])});
  try {
    await page.goto(server.origin + "/memory");
    const queue = page.getByRole("region", {name: "提炼队列"});
    await expect(queue.locator("article > strong")).toHaveCount(3);
    for (const id of [" s ", "s", " ", ""]) {
      const answer = page.waitForResponse(response => {
        const url = new URL(response.url());
        return url.pathname === "/api/memory/consolidation" && !url.searchParams.has("batch_id") && !url.searchParams.has("operation_id");
      });
      await queue.getByLabel("按会话 ID 筛选队列").fill(id);
      await queue.getByRole("button", {name: "筛选队列", exact: true}).click();
      const response = await answer;
      const params = new URL(response.url()).searchParams;
      expect(params.get("session_id")).toBe(id === "" ? null : id);
      const body = await response.json();
      expect(body.sessions.map(s => s.session_id).sort()).toEqual(id === "" ? [" ", " s ", "s"] : [id]);
      // Do not normalize whitespace in the assertion either: these are three
      // separate historical identities, including the all-whitespace one.
      await expect.poll(() => queue.locator("article > strong").allTextContents()).toEqual(body.sessions.map(s => `会话 ${s.session_id}`));
      await expect(queue.getByLabel("按会话 ID 筛选队列")).toHaveValue(id);
    }
  } finally { await server.close(); }
});

test("G2 v4: controlled stream waits for the application's entry read", async ({page}) => {
  const server = await memoryServer();
  let release;
  try {
    const stream = await controlledStream(page, server.origin);
    const held = new Promise(resolve => {release = resolve;});
    let captured = false;
    await page.route("**/api/entry", async route => {
      const response = await route.fetch(); captured = true;
      await held; await route.fulfill({response});
    });
    await page.goto(server.origin + "/memory");
    await expect.poll(() => captured).toBe(true);
    expect(await page.evaluate(() => window.sources.length)).toBe(0);
    let outcome = "waiting";
    const connected = stream.connect().then(() => {outcome = "connected";}, error => {outcome = error.message; throw error;});
    void connected.catch(() => {});
    // This round trip establishes that connect reached the browser while
    // entry is still held; readiness must remain pending, not throw.
    await page.evaluate(() => document.readyState);
    expect(outcome).toBe("waiting");
    release();
    await connected;
    await expect(page.getByRole("tabpanel", {name: "语义记忆"}).getByText("记忆库为空。", {exact: true})).toBeVisible();
  } finally { release?.(); await server.close(); }
});

test("G2 v5: receipt refresh preserves the selected exact legacy scope", async ({page}) => {
  const {execFileSync} = await import("node:child_process");
  const server = await memoryServer({
    prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", SEED_LEGACY, directory]),
  });
  try {
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await panel.getByLabel("新主题").fill("饮食");
    await panel.getByLabel("新事实").fill("legacy scoped fact");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await panel.getByRole("button", {name: "查看详情", exact: true}).click();
    await panel.getByRole("button", {name: "删除", exact: true}).click();
    await panel.getByRole("button", {name: "确认删除", exact: true}).click();
    const receipt = page.getByRole("region", {name: "记忆操作回执"}).locator("article").first();
    await expect(receipt).toContainText("记忆已删除（数据库已确认）");
    await expect(receipt).toContainText("来源关联不完整：确认隔离范围之前遗忘尚未完成。");
    const scopes = receipt.getByRole("group", {name: "待确认的历史范围（服务端快照）"});
    const scope = scopes.getByLabel(/^legacy legacy \/会话\? · 55 个历史组 · 时间未知 至 时间未知 · 修订 1/);
    await expect(scope).toBeVisible();
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    await expect(receipt).toContainText("来源关联不完整");
    await scope.check();
    const checkedNode = await scope.elementHandle();
    const other = await api(page.request, server.origin);
    await other.command({operation_id: "scope-refresh", kind: "semantic", action: "save", payload: {subject: "other", fact: "unrelated invalidation"}});
    await expect.poll(() => checkedNode.evaluate(element => element.isConnected)).toBe(false);
    await expect(scope).toBeChecked();
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    await expect(receipt).toContainText("遗忘完成：条目、索引与受管副本均已清理并核实。");
    await expect(receipt.getByRole("group")).toHaveCount(0);
    await expect(receipt).toContainText("原始会话与删除前的追踪仍可人工查看");
  } finally {
    await server.close();
  }
});

test("G2 v5: late pre-confirmation receipt cannot restore needs-scope progress", async ({page}) => {
  const {execFileSync} = await import("node:child_process");
  const server = await memoryServer({
    prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", SEED_LEGACY, directory]),
  });
  try {
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await panel.getByLabel("新主题").fill("饮食");
    await panel.getByLabel("新事实").fill("legacy scoped fact");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await panel.getByRole("button", {name: "查看详情", exact: true}).click();
    await panel.getByRole("button", {name: "删除", exact: true}).click();
    await panel.getByRole("button", {name: "确认删除", exact: true}).click();
    const receipt = page.getByRole("region", {name: "记忆操作回执"}).locator("article").first();
    await expect(receipt).toContainText("记忆已删除（数据库已确认）");
    await expect(receipt).toContainText("来源关联不完整：确认隔离范围之前遗忘尚未完成。");
    const scopes = receipt.getByRole("group", {name: "待确认的历史范围（服务端快照）"});
    const scope = scopes.getByLabel(/^legacy legacy \/会话\? · 55 个历史组 · 时间未知 至 时间未知 · 修订 1/);
    await expect(scope).toBeVisible();
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    await expect(receipt).toContainText("来源关联不完整");
    let release;
    const held = new Promise(resolve => {release = resolve;});
    let captured = false;
    let delivered = false;
    await page.route("**/api/memory/operations?*", async route => {
      if (captured) return route.continue();
      const response = await route.fetch(); captured = true;
      await held; await route.fulfill({response}); delivered = true;
    });
    const other = await api(page.request, server.origin);
    await other.command({operation_id: "late-scope-refresh", kind: "semantic", action: "save", payload: {subject: "other", fact: "unrelated"}});
    await expect.poll(() => captured).toBe(true);
    await scope.check();
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    await expect.poll(() => page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.operations")).find(e => e.action === "delete")?.forgetting?.state)).toBe("complete");
    await expect(receipt).toContainText("遗忘完成：条目、索引与受管副本均已清理并核实。");
    const watched = await watchText(page, "来源关联不完整");
    release();
    await expect.poll(() => delivered).toBe(true);
    await expect(receipt).toContainText("遗忘完成：条目、索引与受管副本均已清理并核实。");
    await expect(receipt.getByRole("group")).toHaveCount(0);
    expect((await watched()).shown).toBe(false);
    await expect(receipt).toContainText("原始会话与删除前的追踪仍可人工查看");
  } finally {
    await server.close();
  }
});

test("G2 v5: a revised scope requires a new explicit selection", async ({page}) => {
  const {execFileSync} = await import("node:child_process");
  const server = await memoryServer({
    prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", SEED_LEGACY, directory]),
  });
  try {
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await panel.getByLabel("新主题").fill("饮食");
    await panel.getByLabel("新事实").fill("legacy scoped fact");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await panel.getByRole("button", {name: "查看详情", exact: true}).click();
    await panel.getByRole("button", {name: "删除", exact: true}).click();
    await panel.getByRole("button", {name: "确认删除", exact: true}).click();
    const receipt = page.getByRole("region", {name: "记忆操作回执"}).locator("article").first();
    await expect(receipt).toContainText("记忆已删除（数据库已确认）");
    await expect(receipt).toContainText("来源关联不完整：确认隔离范围之前遗忘尚未完成。");
    const scopes = receipt.getByRole("group", {name: "待确认的历史范围（服务端快照）"});
    const scope = scopes.getByLabel(/^legacy legacy \/会话\? · 55 个历史组 · 时间未知 至 时间未知 · 修订 1/);
    await expect(scope).toBeVisible();
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    await expect(receipt).toContainText("来源关联不完整");
    await scope.check();
    await page.route("**/api/memory/operations?*", async route => {
      const response = await route.fetch();
      const body = await response.json();
      // Exercise the read contract's revision boundary without authorizing
      // this fabricated successor on the real server.
      if (body.scopes) body.scopes = body.scopes.map(scope => ({...scope, revision: scope.revision + 1}));
      await route.fulfill({response, json: body});
    });
    const checkedNode = await scope.elementHandle();
    const other = await api(page.request, server.origin);
    await other.command({operation_id: "scope-refresh", kind: "semantic", action: "save", payload: {subject: "other", fact: "unrelated invalidation"}});
    await expect.poll(() => checkedNode.evaluate(element => element.isConnected)).toBe(false);
    const revised = scopes.getByRole("checkbox");
    await expect(revised).not.toBeChecked();
    await expect(scopes).toContainText("修订 2");
    let sent = false;
    await page.route("**/api/memory/forget/actions", route => {sent = true; return route.continue();});
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    expect(sent).toBe(false);
    await expect(receipt).toContainText("来源关联不完整");
  } finally {
    await server.close();
  }
});

test("G2 v6: late abort is ignored and the coalesced query runs", async ({page}) => {
  const {execFileSync} = await import("node:child_process");
  const server = await memoryServer({
    prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", SEED_LEGACY, directory]),
  });
  try {
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await panel.getByLabel("新主题").fill("饮食");
    await panel.getByLabel("新事实").fill("legacy scoped fact");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await panel.getByRole("button", {name: "查看详情", exact: true}).click();
    await panel.getByRole("button", {name: "删除", exact: true}).click();
    await panel.getByRole("button", {name: "确认删除", exact: true}).click();
    const receipt = page.getByRole("region", {name: "记忆操作回执"}).locator("article").first();
    await expect(receipt).toContainText("记忆已删除（数据库已确认）");
    await expect(receipt).toContainText("来源关联不完整：确认隔离范围之前遗忘尚未完成。");
    const scopes = receipt.getByRole("group", {name: "待确认的历史范围（服务端快照）"});
    const scope = scopes.getByLabel(/^legacy legacy \/会话\? · 55 个历史组 · 时间未知 至 时间未知 · 修订 1/);
    await expect(scope).toBeVisible();
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    await expect(receipt).toContainText("来源关联不完整");
    let release;
    const held = new Promise(resolve => {release = resolve;});
    let captured = false;
    let delivered = false;
    let reads = 0;
    await page.route("**/api/memory/operations?*", async route => {
      reads++;
      if (captured) return route.continue();
      const response = await route.fetch(); captured = true;
      await held; await route.abort("failed"); delivered = true;
    });
    const other = await api(page.request, server.origin);
    await other.command({operation_id: "late-scope-refresh", kind: "semantic", action: "save", payload: {subject: "other", fact: "unrelated"}});
    await expect.poll(() => captured).toBe(true);
    await scope.check();
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    await expect.poll(() => page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.operations")).find(e => e.action === "delete")?.forgetting?.state)).toBe("complete");
    await expect(receipt).toContainText("遗忘完成：条目、索引与受管副本均已清理并核实。");
    const watched = await watchText(page, "清理进度暂不可读取");
    release();
    await expect.poll(() => delivered).toBe(true);
    await expect.poll(() => reads).toBeGreaterThan(1);
    await expect(receipt).toContainText("遗忘完成：条目、索引与受管副本均已清理并核实。");
    await expect(receipt.getByRole("group")).toHaveCount(0);
    expect((await watched()).shown).toBe(false);
    await expect(receipt).toContainText("原始会话与删除前的追踪仍可人工查看");
  } finally {
    await server.close();
  }
});


test("G2 v6: late truncated-json is ignored and the coalesced query runs", async ({page}) => {
  const {execFileSync} = await import("node:child_process");
  const server = await memoryServer({
    prepare: directory => execFileSync(".venv/bin/python", ["-B", "-c", SEED_LEGACY, directory]),
  });
  try {
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await panel.getByLabel("新主题").fill("饮食");
    await panel.getByLabel("新事实").fill("legacy scoped fact");
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await panel.getByRole("button", {name: "查看详情", exact: true}).click();
    await panel.getByRole("button", {name: "删除", exact: true}).click();
    await panel.getByRole("button", {name: "确认删除", exact: true}).click();
    const receipt = page.getByRole("region", {name: "记忆操作回执"}).locator("article").first();
    await expect(receipt).toContainText("记忆已删除（数据库已确认）");
    await expect(receipt).toContainText("来源关联不完整：确认隔离范围之前遗忘尚未完成。");
    const scopes = receipt.getByRole("group", {name: "待确认的历史范围（服务端快照）"});
    const scope = scopes.getByLabel(/^legacy legacy \/会话\? · 55 个历史组 · 时间未知 至 时间未知 · 修订 1/);
    await expect(scope).toBeVisible();
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    await expect(receipt).toContainText("来源关联不完整");
    let release;
    const held = new Promise(resolve => {release = resolve;});
    let captured = false;
    let delivered = false;
    let reads = 0;
    await page.route("**/api/memory/operations?*", async route => {
      reads++;
      if (captured) return route.continue();
      const response = await route.fetch(); captured = true;
      await held; await route.fulfill({status:200,contentType:"application/json",body:"{\"result\":"}); delivered = true;
    });
    const other = await api(page.request, server.origin);
    await other.command({operation_id: "late-scope-refresh", kind: "semantic", action: "save", payload: {subject: "other", fact: "unrelated"}});
    await expect.poll(() => captured).toBe(true);
    await scope.check();
    await scopes.getByRole("button", {name: "确认隔离所选范围", exact: true}).click();
    await expect.poll(() => page.evaluate(() => JSON.parse(sessionStorage.getItem("alfred.memory.operations")).find(e => e.action === "delete")?.forgetting?.state)).toBe("complete");
    await expect(receipt).toContainText("遗忘完成：条目、索引与受管副本均已清理并核实。");
    const watched = await watchText(page, "清理进度暂不可读取");
    release();
    await expect.poll(() => delivered).toBe(true);
    await expect.poll(() => reads).toBeGreaterThan(1);
    await expect(receipt).toContainText("遗忘完成：条目、索引与受管副本均已清理并核实。");
    await expect(receipt.getByRole("group")).toHaveCount(0);
    expect((await watched()).shown).toBe(false);
    await expect(receipt).toContainText("原始会话与删除前的追踪仍可人工查看");
  } finally {
    await server.close();
  }
});


test("G2 v6: retained complete deletion rechecks on invalidation and reconnect without polling", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    const stream = await controlledStream(page, server.origin);
    await other.command({operation_id:"reopen-seed",kind:"semantic",action:"save",payload:{subject:"cleanup",fact:"fact"}});
    await page.goto(server.origin + "/memory");
    await stream.connect();
    const panel = page.getByRole("tabpanel", {name:"语义记忆"});
    await panel.getByRole("button", {name:"查看详情",exact:true}).click();
    await panel.getByRole("button", {name:"删除",exact:true}).click();
    await panel.getByRole("button", {name:"确认删除",exact:true}).click();
    const receipt = page.getByRole("region", {name:"记忆操作回执"}).locator("article").first();
    await expect(receipt).toContainText("遗忘完成");
    let reads = 0;
    let reopened = true;
    await page.route("**/api/memory/operations?*", async route => {
      reads++;
      const response = await route.fetch();
      const body = await response.json();
      // Legal read-side progress shape at the HTTP boundary; this does not
      // simulate an actual server-side late provenance association.
      if (reopened && body.forgetting) body.forgetting = {...body.forgetting,state:"cleaning"};
      await route.fulfill({response,json:body});
    });
    await other.command({operation_id:"reopen-wakeup",kind:"semantic",action:"save",payload:{subject:"other",fact:"unrelated"}});
    await stream.patch((await other.get("/api/memory/state")).body.memory_revision);
    await expect(receipt).toContainText("受管副本清理中");
    expect(reads).toBeGreaterThan(0);
    const beforeReconnect = reads;
    reopened = false;
    await stream.emit("error", {});
    await stream.connect();
    await expect(receipt).toContainText("遗忘完成");
    expect(reads).toBeGreaterThan(beforeReconnect);
    const beforeCompleteReconnect = reads;
    await stream.emit("error", {});
    await stream.connect();
    await expect.poll(() => reads).toBeGreaterThan(beforeCompleteReconnect);
    await expect(receipt).toContainText("遗忘完成");
    for (let i = 0; i < 5; i++) {
      await other.get("/api/memory/state");
      await page.evaluate(() => new Promise(requestAnimationFrame));
    }
    // Drain the finite reads through HTTP and browser turns without sending
    // another invalidation. No timer or self-trigger may create more reads.
    const snapshot = reads;
    for (let i = 0; i < 5; i++) {
      await other.get("/api/memory/state");
      await page.evaluate(() => new Promise(requestAnimationFrame));
    }
    expect(reads).toBe(snapshot);
  } finally {await server.close();}
});
