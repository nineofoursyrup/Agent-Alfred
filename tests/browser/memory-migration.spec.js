import {test, expect} from "@playwright/test";
import {api, memoryServer} from "./memory-server.js";

test("S05: a confirmed save cannot clear the next draft or steal its focus", async ({page}) => {
  const server = await memoryServer();
  try {
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name: "语义记忆"});
    await expect(panel.getByText("记忆库为空。", {exact: true})).toBeVisible();
    await panel.getByRole("button", {name:"新建语义记忆",exact:true}).click();
    await panel.getByLabel("新主题", {exact: true}).fill("首次提交");
    await panel.getByLabel("新事实", {exact: true}).fill("已提交的第一份内容");
    let release, received;
    const gate = new Promise(resolve => {release = resolve;});
    const arrived = new Promise(resolve => {received = resolve;});
    await page.route("**/api/memory/commands", async route => {
      const response = await route.fetch();
      received();
      await gate;
      await route.fulfill({response});
    });
    await panel.getByRole("button", {name: "保存", exact: true}).click();
    await arrived;
    await panel.getByLabel("新主题", {exact: true}).fill("下一份草稿");
    const input = panel.getByLabel("新事实", {exact: true});
    await input.fill("等待回执时继续输入");
    release();
    await expect(panel.getByText(/^已保存（ID .+，版本 1）。/)).toBeVisible();
    await expect(input).toHaveValue("等待回执时继续输入");
    await expect(input).toBeFocused();
    const other = await api(page.request, server.origin);
    const {body} = await other.get("/api/memory/records?kind=semantic");
    expect(body.records.map(record => record.fact)).toEqual(["已提交的第一份内容"]);
  } finally {
    await server.close();
  }
});

test("S05: collapsed content preserves drafts while maintenance exposes bounded status", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    await other.command({operation_id:"fold-record",kind:"semantic",action:"save",payload:{subject:"长内容",fact:"长中文正文。".repeat(120)}});
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name:"语义记忆"});
    const create = panel.getByRole("button", {name:"新建语义记忆",exact:true});
    await expect(create).toHaveAttribute("aria-expanded", "false");
    await create.click();
    await panel.getByLabel("新事实", {exact:true}).fill("待保存的新内容");
    await create.click();
    await expect(panel.getByText("新建输入尚未保存。", {exact:true})).toBeVisible();
    await create.click();
    await expect(panel.getByLabel("新事实", {exact:true})).toHaveValue("待保存的新内容");
    await panel.getByRole("button", {name:"查看详情",exact:true}).click();
    const detail = panel.getByLabel("语义记忆详情");
    await detail.getByRole("button", {name:"编辑",exact:true}).click();
    await detail.getByLabel("事实", {exact:true}).fill("详情中的未保存编辑");
    await detail.getByRole("button", {name:"收起详情",exact:true}).click();
    await expect(detail).toContainText("未保存编辑");
    await page.getByRole("tab", {name:"情景记忆",exact:true}).click();
    await page.getByRole("tab", {name:"语义记忆",exact:true}).click();
    await detail.getByRole("button", {name:"展开详情",exact:true}).click();
    await expect(detail.getByLabel("事实", {exact:true})).toHaveValue("详情中的未保存编辑");
    for (const name of ["操作回执／遗忘进度", "提炼队列", "检索统计", "Markdown 镜像"])
      await expect(page.getByRole("button", {name,exact:true})).toHaveAttribute("aria-expanded", "false");
    await expect(page.locator(".memory-summary").filter({hasText:"本标签页尚无记忆操作"})).toBeVisible();
    await expect(page.locator(".memory-summary").filter({hasText:"当前队列页"})).toBeVisible();
  } finally {await server.close();}
});

test("S05: switching the edit target requires an explicit discard and cancellation preserves focus", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    for (const [operation_id, subject] of [["guard-a", "记录甲"], ["guard-b", "记录乙"]])
      expect((await other.command({operation_id, kind:"semantic", action:"save", payload:{subject, fact:subject + "正文"}})).status).toBe(200);
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel", {name:"语义记忆"});
    const list = panel.getByLabel("语义记忆列表");
    await list.locator("article", {hasText:"记录甲"}).getByRole("button", {name:"查看详情",exact:true}).click();
    const detail = panel.getByLabel("语义记忆详情");
    await detail.getByRole("button", {name:"编辑",exact:true}).click();
    const draft = panel.getByLabel("事实", {exact:true});
    await draft.fill("甲的未保存草稿");
    const target = list.locator("article", {hasText:"记录乙"}).getByRole("button", {name:"查看详情",exact:true});
    await target.click();
    const dialog = page.getByRole("dialog", {name:"保留当前编辑？"});
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", {name:"保留当前编辑",exact:true})).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(draft).toHaveValue("甲的未保存草稿");
    await expect(target).toBeFocused();
    await target.click();
    await dialog.getByRole("button", {name:"放弃后继续",exact:true}).click();
    await expect(detail.getByText("记录乙正文", {exact:true})).toBeVisible();
    await expect(draft).toHaveValue("");
  } finally {
    await server.close();
  }
});

test("S05: editing during a save retains the new text and independently filtered detail", async ({page}) => {
  const server = await memoryServer();
  let release;
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({operation_id:"late-edit-seed",kind:"semantic",action:"save",payload:{subject:"保留详情",fact:"版本一"}});
    const id = saved.body.result.memory_id;
    await page.goto(server.origin + "/memory");
    const panel=page.getByRole("tabpanel",{name:"语义记忆"}), detail=panel.getByLabel("语义记忆详情");
    await panel.getByRole("button",{name:"查看详情",exact:true}).click();
    await detail.getByRole("button",{name:"编辑",exact:true}).click();
    const draft=detail.getByLabel("事实",{exact:true});
    await draft.fill("已经提交的修改");
    let received;
    const gate=new Promise(resolve=>{release=resolve;}), arrived=new Promise(resolve=>{received=resolve;});
    await page.route("**/api/memory/commands",async route=>{
      const response=await route.fetch();received();await gate;await route.fulfill({response});
    });
    await detail.getByRole("button",{name:"保存修改",exact:true}).click();
    await arrived;
    // Returning to the pre-submit value is a new intent once B was submitted.
    await draft.fill("版本一");
    await page.locator('nav a[href="/overview"]').click();
    await expect(page.getByRole('button',{name:'留在此页',exact:true})).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(draft).toHaveValue("版本一");
    await draft.focus();
    release();
    await expect(detail.getByText("此前修改已确认保存；提交后新增的编辑尚未保存。",{exact:true})).toBeVisible();
    await expect(draft).toHaveValue("版本一");
    await expect(draft).toBeFocused();
    expect((await other.get(`/api/memory/record?kind=semantic&id=${id}`)).body.record.fact).toBe("已经提交的修改");
    await panel.getByLabel("文本",{exact:true}).fill("此查询没有结果");
    await panel.getByRole("button",{name:"搜索",exact:true}).click();
    await expect(panel.getByText("没有匹配的记忆。",{exact:true})).toBeVisible();
    await expect(detail).toContainText("独立详情，不在当前查询已加载结果中");
    await expect(draft).toHaveValue("版本一");
    await page.unroute("**/api/memory/commands");
    await detail.getByRole("button",{name:"保存修改",exact:true}).click();
    await expect(detail.getByText("已更新到版本 3。",{exact:true})).toBeVisible();
    expect((await other.get(`/api/memory/record?kind=semantic&id=${id}`)).body.record.fact).toBe("版本一");
  } finally {release?.();await server.close();}
});

test("S05: real forgetting invalidates folded details, drafts and mirror previews before late reads return", async ({page}) => {
  const server = await memoryServer();
  let release;
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({operation_id:"hidden-seed",kind:"semantic",action:"save",payload:{subject:"受保护长内容",fact:"受保护的旧正文".repeat(40)}});
    const id = saved.body.result.memory_id;
    await page.setViewportSize({width:390,height:844});
    await page.goto(server.origin + "/memory");
    const panel=page.getByRole("tabpanel",{name:"语义记忆",includeHidden:true}), detail=panel.getByLabel("语义记忆详情");
    await panel.getByRole("button",{name:"查看详情",exact:true}).click();
    await detail.getByRole("button",{name:"编辑",exact:true}).click();
    const draft=detail.getByLabel("事实",{exact:true});
    await draft.fill("可复用的编辑副本");
    await detail.getByRole("button",{name:"收起详情",exact:true}).click();
    const mirrorToggle=page.getByRole("button",{name:"Markdown 镜像",exact:true});
    await mirrorToggle.click();
    const mirror=page.getByRole("region",{name:"Markdown 镜像"}).locator('[data-mirror="facts"]');
    await mirror.getByRole("button",{name:"预览",exact:true}).click();
    await expect(mirror.locator("pre")).toContainText("受保护的旧正文");
    await mirrorToggle.click();
    let received;
    const gate=new Promise(resolve=>{release=resolve;}), arrived=new Promise(resolve=>{received=resolve;});
    let held=false;
    await page.route("**/api/memory/record?*",async route=>{
      if (held) return route.continue();
      held=true;const response=await route.fetch();received();await gate;await route.fulfill({response});
    });
    await other.command({operation_id:"hidden-trigger",kind:"semantic",action:"save",payload:{subject:"其他独立记忆",fact:"不应连带删除"}});
    await arrived;
    await page.getByRole("button",{name:"打开主对话",exact:true}).click();
    expect((await other.command({operation_id:"hidden-delete",kind:"semantic",action:"delete",payload:{id},expected_version:1})).body.result.status).toBe("deleted");
    await expect(detail.locator(".memory-summary")).toContainText("记录已不存在");
    await expect(draft).toHaveValue("");
    await expect(page.locator(".memory-page")).not.toContainText("受保护的旧正文");
    release();
    await page.getByRole("button",{name:"收起主对话",exact:true}).click();
    await expect(detail.getByRole("button",{name:"展开详情",exact:true})).toHaveAttribute("aria-expanded","false");
    await expect(mirrorToggle).toHaveAttribute("aria-expanded","false");
    await detail.getByRole("button",{name:"展开详情",exact:true}).click();
    await expect(detail).toContainText("记录已不存在");
    await expect(page.locator(".memory-page")).not.toContainText("受保护的旧正文");
    await expect(panel.getByText("不应连带删除",{exact:true})).toBeVisible();
    expect((await other.get(`/api/memory/record?kind=semantic&id=${id}`)).status).toBe(404);
  } finally {release?.();await server.close();}
});

for (const collapseBeforeFailure of [true, false])
test(`S05 Spec-F1: mirror preview ${collapseBeforeFailure ? "loading then collapse" : "failure then collapse"} retains its own error until a current read succeeds`, async ({page}) => {
  const server = await memoryServer();
  const deferred = () => {let resolve;const promise = new Promise(done=>{resolve=done;});return {promise,resolve};};
  let gate = deferred(), arrived = deferred(), mode = "fail", completed = 0;
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({operation_id:"preview-seed",kind:"semantic",action:"save",payload:{subject:"镜像私有主题",fact:"镜像受保护正文"}});
    await page.goto(server.origin + "/memory");
    const toggle = page.getByRole("button",{name:"Markdown 镜像",exact:true});
    const summary = toggle.locator("xpath=../..").locator(".memory-summary");
    const mirrors = page.getByRole("region",{name:"Markdown 镜像",includeHidden:true});
    const facts = mirrors.locator('[data-mirror="facts"]');
    const focus = page.getByRole("tabpanel",{name:"语义记忆"}).getByLabel("文本",{exact:true});
    await toggle.click();
    await facts.getByRole("button",{name:"预览",exact:true}).click();
    await expect(facts.locator("pre")).toContainText("镜像受保护正文");
    await page.route("**/api/memory/mirrors?name=facts&preview=1",async route=>{
      if (mode === "pass") return route.continue();
      const ownMode = mode, ownGate = gate;
      arrived.resolve();
      await ownGate.promise;
      if (ownMode === "recover") await route.continue();
      else await route.fulfill({status:503,json:{error:{code:"storage_read_failed"}}});
      completed++;
    });
    await facts.getByRole("button",{name:"预览",exact:true}).click();
    await arrived.promise;
    const loading = await summary.textContent();
    if (collapseBeforeFailure) await toggle.click();
    gate.resolve();
    await expect(facts.locator("pre")).toHaveText("当前镜像未核验或不可读，不显示旧内容。");
    if (!collapseBeforeFailure) await toggle.click();
    await focus.focus();
    await expect(summary).toContainText("事实预览：读取失败（storage_read_failed）");
    expect(loading).toContain("事实预览：读取中");
    await expect(summary).toContainText("事实：已同步并核验");
    await expect(summary).not.toContainText("情景预览：读取失败");
    await expect(summary).not.toContainText("镜像受保护正文");
    await expect(facts).not.toContainText("镜像受保护正文");
    await expect(toggle).toHaveAttribute("aria-expanded","false");
    await expect(focus).toBeFocused();
    gate = deferred();arrived = deferred();
    if (collapseBeforeFailure) {
      // A real deletion invalidates the list, but that list success cannot
      // clear a previous preview failure while its new preview read is held.
      mode = "recover";
      const before = (await other.get("/api/memory/state")).body;
      expect((await other.command({operation_id:"preview-delete",kind:"semantic",action:"delete",payload:{id:saved.body.result.memory_id},expected_version:1})).body.result.status).toBe("deleted");
      await arrived.promise;
      const after = (await other.get("/api/memory/state")).body;
      expect(after.process_instance_id).toBe(before.process_instance_id);
      expect(after.memory_revision).toBeGreaterThan(before.memory_revision);
      await expect(summary).toContainText("事实：已同步并核验");
      await expect(summary).toContainText("事实预览：读取中");
      await expect(summary).toContainText("storage_read_failed");
      await expect(page.locator(".memory-page")).not.toContainText("镜像受保护正文");
      mode = "pass";gate.resolve();
      await expect(summary).toContainText("事实预览：已核验");
      await expect(summary).not.toContainText("storage_read_failed");
      await expect(toggle).toHaveAttribute("aria-expanded","false");
      await expect(focus).toBeFocused();
      await toggle.click();
      await expect(facts.locator("pre")).toContainText("# Facts");
      await expect(facts).not.toContainText("镜像受保护正文");
    } else {
      // Two reads for the same mirror share the list revision. A newer
      // authoritative success owns both the preview body and its status.
      mode = "late-fail";
      await toggle.click();
      await facts.getByRole("button",{name:"预览",exact:true}).click();
      await arrived.promise;
      mode = "pass";
      await facts.getByRole("button",{name:"预览",exact:true}).click();
      await expect(summary).toContainText("事实预览：已核验");
      await expect(summary).not.toContainText("storage_read_failed");
      await toggle.click();await focus.focus();
      const earlier = completed;gate.resolve();
      await expect.poll(()=>completed).toBeGreaterThan(earlier);
      await expect(summary).toContainText("事实预览：已核验");
      await expect(summary).not.toContainText("storage_read_failed");
      await expect(toggle).toHaveAttribute("aria-expanded","false");
      await expect(focus).toBeFocused();
    }
  } finally {gate.resolve();await server.close();}
});

test("S05: a folded receipt exposes uncertainty without automatic expansion or focus movement", async ({page}) => {
  const server = await memoryServer();
  try {
    await page.goto(server.origin + "/memory");
    const panel=page.getByRole("tabpanel",{name:"语义记忆"});
    await panel.getByRole("button",{name:"新建语义记忆",exact:true}).click();
    await panel.getByLabel("新主题").fill("回执未知");
    await panel.getByLabel("新事实").fill("已入库但响应丢失");
    let writes=0;
    await page.route("**/api/memory/operations?*",route=>route.fulfill({status:503,json:{error:{code:"storage_read_failed"}}}));
    await page.route("**/api/memory/commands",async route=>{writes++;await route.fetch();await route.abort("connectionreset");});
    const save=panel.getByRole("button",{name:"保存",exact:true});
    await save.click();
    await panel.getByLabel("文本",{exact:true}).focus();
    const toggle=page.getByRole("button",{name:"操作回执／遗忘进度",exact:true});
    await expect(toggle.locator("xpath=../..").locator(".memory-summary")).toContainText("结果待确认 1");
    await expect(toggle).toHaveAttribute("aria-expanded","false");
    await expect(panel.getByLabel("文本",{exact:true})).toBeFocused();
    expect(writes).toBe(1);
    const other=await api(page.request,server.origin);
    expect((await other.get("/api/memory/records?kind=semantic")).body.records).toHaveLength(1);
    await toggle.click();
    await expect(page.getByRole("region",{name:"记忆操作回执"})).toContainText("查询失败（storage_read_failed）：仍未确认。");
  } finally {await server.close();}
});

test("S05 ST-01: folded completed deletion exposes a failed progress read and clears it only after verification", async ({page}) => {
  const server = await memoryServer();
  try {
    const other = await api(page.request, server.origin);
    const saved = await other.command({operation_id:"summary-seed",kind:"semantic",action:"save",payload:{subject:"已删除的私有主题",fact:"已删除的私有正文"}});
    const id = saved.body.result.memory_id;
    await page.goto(server.origin + "/memory");
    const panel = page.getByRole("tabpanel",{name:"语义记忆"});
    await panel.getByRole("button",{name:"查看详情",exact:true}).click();
    const detail = page.getByLabel("语义记忆详情");
    let writes = 0;
    await page.route("**/api/memory/commands",route=>{writes++;return route.continue();});
    await detail.getByRole("button",{name:"删除",exact:true}).click();
    await detail.getByRole("button",{name:"确认删除",exact:true}).click();
    const receipts = page.getByRole("region",{name:"记忆操作回执",includeHidden:true});
    await expect(receipts).toContainText("遗忘完成：条目、索引与受管副本均已清理并核实。");
    const operation = await receipts.locator("article").getAttribute("data-operation");
    const original = (await other.get(`/api/memory/operations?operation_id=${operation}`)).body;
    const before = (await other.get("/api/memory/state")).body;
    const toggle = page.getByRole("button",{name:"操作回执／遗忘进度",exact:true});
    const summary = toggle.locator("xpath=../..").locator(".memory-summary");
    const focus = panel.getByLabel("文本",{exact:true});
    await focus.focus();
    let failRead = true;
    const failures = [];
    await page.route("**/api/memory/operations?*",async route=>{
      if (!failRead) return route.continue();
      failures.push(new URL(route.request().url()).searchParams.get("operation_id"));
      await route.fulfill({status:503,json:{error:{code:"storage_read_failed"}}});
    });
    await other.command({operation_id:"summary-revision",kind:"semantic",action:"save",payload:{subject:"触发真实修订",fact:"无关的新记录"}});
    await expect(receipts).toContainText("清理进度暂不可读取（storage_read_failed）。");
    const after = (await other.get("/api/memory/state")).body;
    expect(before.process_instance_id).toEqual(expect.any(String));
    expect(after.process_instance_id).toBe(before.process_instance_id);
    expect(after.memory_revision).toBeGreaterThan(before.memory_revision);
    expect(new Set(failures)).toEqual(new Set([operation]));
    await expect(toggle).toHaveAttribute("aria-expanded","false");
    await expect(receipts).toBeHidden();
    await expect(focus).toBeFocused();
    await expect(summary).toBeVisible();
    await expect(summary).toContainText("清理进度暂不可读取（storage_read_failed）");
    await expect(summary).toContainText("当前进度待核验");
    await expect(summary).toContainText("上次已读遗忘完成");
    await expect(summary).not.toContainText("已删除的私有");
    await expect(page.locator(".memory-page")).not.toContainText("已删除的私有");
    failRead = false;
    await other.command({operation_id:"summary-recovery",kind:"semantic",action:"save",payload:{subject:"核验恢复触发",fact:"另一条无关记录"}});
    await expect(summary).not.toContainText("storage_read_failed");
    await expect(summary).not.toContainText("当前进度待核验");
    await expect(summary).toContainText("遗忘未完成 0");
    await expect(toggle).toHaveAttribute("aria-expanded","false");
    await expect(receipts).toBeHidden();
    await expect(focus).toBeFocused();
    const verified = (await other.get(`/api/memory/operations?operation_id=${operation}`)).body;
    expect(verified.result).toEqual(original.result);
    expect(verified.forgetting.state).toBe("complete");
    expect((await other.get(`/api/memory/record?kind=semantic&id=${id}`)).status).toBe(404);
    expect(writes).toBe(1);
  } finally {await server.close();}
});

test("S05: keyboard-only creation and detail return keep every primary control reachable", async ({page}) => {
  const server=await memoryServer();
  try {
    await page.setViewportSize({width:320,height:800});
    await page.emulateMedia({reducedMotion:"reduce"});
    await page.goto(server.origin+"/memory");
    async function tabTo(name) {
      for(let i=0;i<60;i++) {
        await page.keyboard.press("Tab");
        if(await page.evaluate(label=>document.activeElement?.textContent===label,name))return;
      }
      throw new Error(`Keyboard cannot reach ${name}`);
    }
    await tabTo("新建语义记忆");await page.keyboard.press("Enter");
    await page.keyboard.press("Tab");
    await expect(page.getByLabel("新主题",{exact:true})).toBeFocused();
    await page.keyboard.insertText("键盘操作的长中文主题");await page.keyboard.press("Tab");
    await page.keyboard.insertText("键盘输入内容，仅使用模拟文本输入，不冒充真实中文输入法。");
    await page.keyboard.press("Tab");await page.keyboard.press("Enter");
    await expect(page.getByText(/^已保存（ID .+，版本 1）。/)).toBeVisible();
    await tabTo("查看详情");await page.keyboard.press("Enter");
    const detail=page.getByLabel("语义记忆详情");
    await expect(detail.getByRole("heading",{name:"记录详情",exact:true})).toBeFocused();
    await tabTo("编辑");await page.keyboard.press("Enter");
    await expect(detail.getByLabel("事实",{exact:true})).toBeFocused();
    await page.keyboard.press("Tab");await page.keyboard.press("Tab");
    await expect(detail.getByRole("button",{name:"放弃草稿",exact:true})).toBeFocused();
    await page.keyboard.press("Enter");
    await tabTo("回到列表");await page.keyboard.press("Enter");
    await expect(page.getByRole("button",{name:"查看详情",exact:true})).toBeFocused();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth===innerWidth)).toBe(true);
  }finally{await server.close();}
});

test("S05: leaving an in-flight submit guards only new unsubmitted input and keeps the original receipt", async ({page}) => {
  const server=await memoryServer();let release;
  try {
    await page.goto(server.origin+"/memory");
    const panel=page.getByRole("tabpanel",{name:"语义记忆"});
    await panel.getByRole("button",{name:"新建语义记忆",exact:true}).click();
    await panel.getByLabel("新主题").fill("在途请求");
    const draft=panel.getByLabel("新事实");await draft.fill("已经提交的正文");
    let arrived;const requested=new Promise(resolve=>{arrived=resolve;}),gate=new Promise(resolve=>{release=resolve;});
    let writes=0;
    await page.route("**/api/memory/commands",async route=>{writes++;const response=await route.fetch();arrived();await gate;await route.fulfill({response});});
    await panel.getByRole("button",{name:"保存",exact:true}).click();await requested;
    await draft.fill("提交后另加的未提交正文");
    await page.getByRole("link",{name:"收件箱",exact:true}).click();
    const guard=page.getByRole("dialog");await expect(guard).toBeVisible();
    await page.keyboard.press("Escape");await expect(draft).toHaveValue("提交后另加的未提交正文");
    await draft.fill("已经提交的正文");
    await page.getByRole("link",{name:"收件箱",exact:true}).click();
    await expect(page).toHaveURL(/\/inbox$/);
    await expect(guard).toHaveCount(0);
    release();
    await page.getByRole("link",{name:"记忆",exact:true}).click();
    await page.getByRole("button",{name:"操作回执／遗忘进度",exact:true}).click();
    await expect(page.getByRole("region",{name:"记忆操作回执"})).toContainText("已保存");
    expect(writes).toBe(1);
    const other=await api(page.request,server.origin);expect((await other.get("/api/memory/records?kind=semantic")).body.records).toHaveLength(1);
  }finally{release?.();await server.close();}
});
