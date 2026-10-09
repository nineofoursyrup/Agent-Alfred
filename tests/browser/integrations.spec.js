import {test, expect} from "@playwright/test";
import {spawn} from "node:child_process";
import {createInterface} from "node:readline";
import {once} from "node:events";

async function server() {
  const child = spawn(".venv/bin/python", ["tests/browser/integrations_server.py"]);
  const lines = createInterface({input: child.stdout});
  let errors = "";
  child.stderr.on("data", d => {errors += d;});
  const value = await Promise.race([
    once(lines, "line").then(([line]) => JSON.parse(line)),
    once(child, "exit").then(() => {throw new Error(errors);}),
  ]);
  return {...value, async close() {const done = once(child,"exit"); child.kill("SIGTERM"); await done; lines.close(); if (child.exitCode !== 0) throw new Error(errors);}};
}

async function control(s, body) {
  return (await fetch(s.control, body ? {method:"POST", body: JSON.stringify(body)} : {})).json();
}

// Both tabs share actual Host mutation ownership, observations, and SQLite.
test("CE-02 CE-03 real double-tab probe, busy reread, stale success and key rotation", async ({browser}) => {
  const s = await server();
  const context = await browser.newContext();
  try {
    const a = await context.newPage(), b = await context.newPage();
    const origin = s.entry.url || `http://127.0.0.1:${s.entry.port}`;
    await a.goto(origin + "/connections"); await b.goto(origin + "/connections");
    const card = p => p.locator('[data-integration="tavily"]');
    await expect(card(a)).toContainText("已配置未测试");
    expect((await control(s)).requests).toHaveLength(0);
    await control(s, {block: true});
    await card(a).getByRole("button", {name:"测试连接"}).click();
    await expect.poll(async () => (await control(s)).entered).toBe(true);
    await card(b).getByRole("button", {name:"测试连接"}).click();
    await expect(b.getByText("mutation_in_flight", {exact:true})).toBeVisible();
    await b.getByRole("button", {name:"重新读取 .env"}).click();
    await expect(b.getByText("mutation_in_flight", {exact:true}).first()).toBeVisible();
    await control(s, {release:true});
    await expect(card(a)).toContainText("已连接");
    await expect(card(b)).toContainText("已连接");
    expect((await control(s)).requests).toHaveLength(1);
    // Hold a real cached response in browser transport while B publishes new key.
    let resolveHeld, resolveCaptured;
    const held = new Promise(r => {resolveHeld = r;});
    const captured = new Promise(r => {resolveCaptured = r;});
    await a.route("**/api/connections/probe", async route => {
      const response = await route.fetch(); resolveCaptured(); await held;
      await route.fulfill({response});
    });
    await card(a).getByRole("button", {name:"测试连接"}).click(); await captured;
    await control(s, {key:"browser-key-replacement"});
    await b.getByRole("button", {name:"重新读取 .env"}).click();
    await expect(card(b)).toContainText("末四位 ment");
    await expect(card(a)).toContainText("末四位 ment");
    resolveHeld();
    await expect(card(a)).toContainText("已配置未测试");
    await expect(card(a)).not.toContainText("已连接");
    expect((await control(s)).requests).toHaveLength(1);
  } finally {try {await context.close();} finally {await s.close();}}
});

test("CE-03 CE-08 CE-09 MainBar uses real search, safe text, Ops credits, newer error wins", async ({browser}) => {
  const s = await server(); const context = await browser.newContext();
  try {
    const a = await context.newPage(), b = await context.newPage();
    const origin = `http://127.0.0.1:${s.entry.port}`;
    await a.goto(origin + "/connections");
    await a.locator('[data-integration="tavily"]').getByRole("button", {name:"测试连接"}).click();
    await expect(a.locator('[data-integration="tavily"]')).toContainText("已连接");
    await b.goto(origin + "/tools");
    await b.locator("article").filter({has:b.getByRole("heading",{name:"web_search",exact:true})}).getByRole("button",{name:"展开详情",exact:true}).click();
    await b.getByRole("combobox", {name:"web_search 授权草稿"}).selectOption("allowed");
    await b.locator("article").filter({has: b.getByRole("heading", {name:"web_search", exact:true})}).getByRole("button", {name:"保存授权", exact:true}).click();
    await expect(b.locator("article").filter({has: b.getByRole("heading", {name:"web_search", exact:true})})).toContainText("模型暴露：real");
    await b.getByRole("button", {name:"新建会话", exact:true}).click();
    await b.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    let external = 0; b.on("request", r => {if (r.url().includes("source.invalid")) external++;});
    await b.getByRole("textbox", {name:"消息"}).fill("搜索验收");
    await b.getByRole("button", {name:"发送", exact:true}).click();
    const chat = b.getByRole("region", {name:"主对话"});
    await expect(chat).toContainText("<script>window.injected=true</script>");
    expect(await b.evaluate(() => window.injected)).toBeUndefined();
    expect(await chat.locator("img,script").count()).toBe(0); expect(external).toBe(0);
    await b.goto(origin + "/ops");
    await expect(b.getByRole("region", {name:"账目汇总"})).toContainText("tavily");
    await expect(b.getByRole("region", {name:"账目汇总"})).toContainText("0.125 credits");
    // New observation on the same key invalidates the old successful probe.
    let deliver, captured;
    const held = new Promise(r => {deliver=r;}); const received = new Promise(r=>{captured=r;});
    await a.route("**/api/connections/probe", async route => {const response = await route.fetch(); captured(); await held; await route.fulfill({response});});
    await a.locator('[data-integration="tavily"]').getByRole("button",{name:"测试连接"}).click(); await received;
    await control(s, {status:401});
    await b.getByRole("textbox", {name:"消息"}).fill("搜索验收");
    await b.getByRole("button", {name:"发送", exact:true}).click();
    await expect(a.locator('[data-integration="tavily"]')).toContainText("authentication_failed");
    deliver();
    await expect(a.locator('[data-integration="tavily"]')).not.toContainText("连接：已连接");
    expect((await control(s)).requests.filter(p=>p==="/search")).toHaveLength(2);
  } finally {try {await context.close();} finally {await s.close();}}
});

test("CE-03 Connections resets across real Host restart and rejects old process response", async ({browser}) => {
  let s = await server(); const context = await browser.newContext();
  let deliver;
  try {
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${s.entry.port}/connections`);
    const card = page.locator('[data-integration="tavily"]');
    await card.getByRole("button", {name:"测试连接"}).click();
    await expect(card).toContainText("连接：已连接");
    let captured; const held = new Promise(r => {deliver=r;});
    const received = new Promise(r=>{captured=r;});
    await page.route("**/api/connections/probe", async route => {const response=await route.fetch(); captured(); await held; await route.fulfill({response});});
    await card.getByRole("button", {name:"测试连接"}).click(); await received;
    await s.close(); s = await server();
    await page.evaluate(()=>window.dispatchEvent(new Event("focus")));
    await expect(card).toContainText("已配置未测试");
    deliver();
    await expect(card).not.toContainText("连接：已连接");
    expect((await control(s)).requests).toHaveLength(0);
  } finally {deliver?.(); try {await context.close();} finally {await s.close();}}
});

test("CE-11 late cached success cannot erase a newer real dotenv failure", async ({browser}) => {
  const s = await server(); const context = await browser.newContext();
  let deliver;
  try {
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${s.entry.port}/connections`);
    const card = page.locator('[data-integration="tavily"]');
    await card.getByRole("button", {name:"测试连接"}).click();
    await expect(card).toContainText("已连接");
    let captured; const held = new Promise(r=>{deliver=r;}); const received = new Promise(r=>{captured=r;});
    await page.route("**/api/connections/probe", async route => {const response=await route.fetch(); captured(); await held; await route.fulfill({response});});
    await card.getByRole("button", {name:"测试连接"}).click(); await received;
    await control(s, {key:"'unterminated"});
    await page.getByRole("button", {name:"重新读取 .env"}).click();
    await expect(page.getByText("dotenv_reload_failed",{exact:true})).toBeVisible();
    deliver();
    await expect(page.getByText("dotenv_reload_failed",{exact:true})).toBeVisible();
    expect((await control(s)).requests).toHaveLength(1);
  } finally {deliver?.(); try {await context.close();} finally {await s.close();}}
});

test("CE-08 rotated old and new secrets are redacted before real SSE delivery", async ({browser}) => {
  const s = await server(); const context = await browser.newContext();
  try {
    const page = await context.newPage();
    const cdp = await context.newCDPSession(page);
    const events = [];
    await cdp.send("Network.enable");
    cdp.on("Network.eventSourceMessageReceived", e => events.push(e.data));
    await page.goto(`http://127.0.0.1:${s.entry.port}/connections`);
    await expect(page.locator('[data-integration="tavily"]')).toContainText("末四位 inal");
    await control(s, {key:"browser-key-replacement", echo_keys:true});
    await page.getByRole("button", {name:"重新读取 .env"}).click();
    await expect(page.locator('[data-integration="tavily"]')).toContainText("末四位 ment");
    await page.getByRole("link", {name:"工具", exact:true}).click();
    await page.locator("article").filter({has:page.getByRole("heading",{name:"web_search",exact:true})}).getByRole("button",{name:"展开详情",exact:true}).click();
    await page.getByRole("combobox", {name:"web_search 授权草稿"}).selectOption("allowed");
    const tool = page.locator("article").filter({has:page.getByRole("heading",{name:"web_search",exact:true})});
    await tool.getByRole("button", {name:"保存授权",exact:true}).click();
    await expect(tool).toContainText("模型暴露：real");
    await page.getByRole("button",{name:"新建会话",exact:true}).click();
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    await page.getByRole("textbox",{name:"消息"}).fill("搜索验收");
    await page.getByRole("button",{name:"发送",exact:true}).click();
    await expect(page.getByRole("region",{name:"主对话"})).toContainText("*** ***");
    expect(events.some(s=>s.includes("tool.finished"))).toBe(true);
    expect(events.join("")).not.toContain("browser-key-original");
    expect(events.join("")).not.toContain("browser-key-replacement");
    expect(await page.locator("body").innerText()).not.toContain("browser-key-original");
    expect(await page.locator("body").innerText()).not.toContain("browser-key-replacement");
  } finally {try {await context.close();} finally {await s.close();}}
});

test("CE-04 CE-05 over-limit reported numbers keep results, missing balance and readable Ops", async ({browser}) => {
  const s = await server(); const context = await browser.newContext();
  try {
    await control(s, {large_number:true});
    const page = await context.newPage();
    const origin = `http://127.0.0.1:${s.entry.port}`;
    await page.goto(origin + "/connections");
    const card = page.locator('[data-integration="tavily"]');
    await card.getByRole("button", {name:"测试连接"}).click();
    await expect(card).toContainText("已连接");
    await expect(card).toContainText("key 套餐用量：未报告");
    await page.goto(origin + "/tools");
    const tool = page.locator("article").filter({has:page.getByRole("heading", {name:"web_search", exact:true})});
    await tool.getByRole("button",{name:"展开详情",exact:true}).click();
    await tool.getByRole("combobox", {name:"web_search 授权草稿"}).selectOption("allowed");
    await tool.getByRole("button", {name:"保存授权", exact:true}).click();
    await expect(tool).toContainText("模型暴露：real");
    await page.getByRole("button", {name:"新建会话", exact:true}).click();
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    await page.getByRole("textbox", {name:"消息"}).fill("搜索验收");
    await page.getByRole("button", {name:"发送", exact:true}).click();
    await expect(page.getByRole("region", {name:"主对话"})).toContainText("https://source.invalid/");
    await page.goto(origin + "/ops");
    const totals = page.getByRole("region", {name:"账目汇总"});
    await expect(totals).toContainText('"unknown":1');
    await expect(totals).toContainText('"reported":0');
    expect((await control(s)).requests).toEqual(["/usage", "/search"]);
  } finally {try {await context.close();} finally {await s.close();}}
});

test("CI-01 real busy probe receipt survives a completed focus refresh", async ({browser}) => {
  const s = await server(); const context = await browser.newContext();
  let deliver;
  try {
    const a = await context.newPage(), b = await context.newPage();
    const origin = `http://127.0.0.1:${s.entry.port}`;
    await a.goto(origin + "/connections"); await b.goto(origin + "/connections");
    const card = p => p.locator('[data-integration="tavily"]');
    await expect(card(a).getByRole("button", {name:"测试连接"})).toBeEnabled();
    await expect(card(b).getByRole("button", {name:"测试连接"})).toBeEnabled();
    await control(s, {block:true});
    await card(a).getByRole("button", {name:"测试连接"}).click();
    await expect.poll(async () => (await control(s)).entered).toBe(true);
    let captured;
    const received = new Promise(resolve => {captured=resolve;});
    const held = new Promise(resolve => {deliver=resolve;});
    await b.route("**/api/connections/probe", async route => {
      const response = await route.fetch();
      expect(response.status()).toBe(409);
      expect(await response.json()).toMatchObject({code:"mutation_in_flight"});
      captured(); await held; await route.fulfill({response});
    });
    await card(b).getByRole("button", {name:"测试连接"}).click(); await received;
    const refreshed = b.waitForResponse(response => new URL(response.url()).pathname === "/api/connections");
    await b.evaluate(() => window.dispatchEvent(new Event("focus")));
    const snapshot = await (await refreshed).json();
    expect(snapshot.process_instance_id).toBeTruthy();
    expect(typeof snapshot.integration_revision).toBe("number");
    deliver();
    await expect(b.getByText("mutation_in_flight", {exact:true})).toBeVisible();
    expect((await control(s)).requests).toEqual(["/usage"]);
  } finally {deliver?.(); await control(s, {release:true}); try {await context.close();} finally {await s.close();}}
});

test("STD-V5-01 model probe success survives a later real reread refusal", async ({browser}) => {
  const s = await server(); const context = await browser.newContext();
  try {
    const page = await context.newPage();
    const origin = `http://127.0.0.1:${s.entry.port}`;
    await page.goto(origin + "/connections");
    const card = page.locator('[data-endpoint="openai"]');
    await expect(card).toContainText("已配置未测试");
    const initial = await (await page.request.get(origin + "/api/connections")).json();
    await control(s, {block:true});
    const successful = page.waitForResponse(response => response.url().endsWith("/api/connections/probe"));
    await card.getByRole("button", {name:"验证凭据"}).click();
    await expect.poll(async () => (await control(s)).entered).toBe(true);
    const refused = page.waitForResponse(response => response.url().endsWith("/api/connections/reread"));
    await page.getByRole("button", {name:"重新读取 .env"}).click();
    expect((await refused).status()).toBe(409);
    await expect(page.getByText("mutation_in_flight", {exact:true})).toBeVisible();
    await control(s, {release:true});
    const response = await successful;
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.integration_revision).toBe(initial.integration_revision);
    expect(body.connections_revision).toBeGreaterThan(initial.connections_revision);
    expect(body.endpoints[0].observation).toMatchObject({state:"connected", checked_via:"auth_probe"});
    await expect(card).toContainText("auth_probe");
    await expect(card).toContainText("连接：已连接");
    await expect(page.getByText("mutation_in_flight", {exact:true})).toBeVisible();
    expect((await control(s)).requests).toEqual(["/auth-probe"]);
  } finally {await control(s, {release:true}); try {await context.close();} finally {await s.close();}}
});

for (const restarted of [false, true]) {
  for (const late of ["409", "read failure"]) {
    test(`SPEC-V5-01 late ${late} preserves configuration suspension and recovery (${restarted ? "new" : "same"} process)`, async ({browser}) => {
      let s = await server(); const context = await browser.newContext();
      let deliver;
      try {
        const a = await context.newPage(), b = await context.newPage();
        const origin = `http://127.0.0.1:${s.entry.port}`;
        await a.goto(origin + "/connections"); await b.goto(origin + "/connections");
        const card = p => p.locator('[data-integration="tavily"]');
        await expect(card(a)).toContainText("已配置未测试");
        await expect(card(b)).toContainText("已配置未测试");
        await control(s, {block:true});
        await card(a).getByRole("button", {name:"测试连接"}).click();
        await expect.poll(async () => (await control(s)).entered).toBe(true);
        let captured;
        const received = new Promise(resolve => {captured=resolve;});
        const held = new Promise(resolve => {deliver=resolve;});
        let capturedOnce = false;
        await b.route(late === "409" ? "**/api/connections/probe" : "**/api/connections", async route => {
          if (capturedOnce) {await route.continue(); return;}
          capturedOnce = true;
          const response = await route.fetch(); expect(response.status()).toBe(late === "409" ? 409 : 200);
          captured(); await held;
          if (late === "409") await route.fulfill({response});
          else await route.abort("failed");
        });
        if (late === "409") await card(b).getByRole("button", {name:"测试连接"}).click();
        else await b.evaluate(() => window.dispatchEvent(new Event("focus")));
        await received;
        await control(s, {release:true}); await expect(card(a)).toContainText("连接：已连接");
        const prior = await (await a.request.get(origin + "/api/connections")).json();
        if (restarted) {
          await s.close(); s = await server();
          await a.goto(origin + "/connections");
          await expect(card(a)).toContainText("已配置未测试");
          await expect(a.getByRole("button", {name:"新建会话", exact:true})).toBeEnabled();
        }
        await control(s, {key:"replacement-spec-key", fault_publication:true});
        const failed = a.waitForResponse(response => response.url().endsWith("/api/connections/reread"));
        await a.getByRole("button", {name:"重新读取 .env"}).click();
        expect((await failed).status()).toBe(400);
        // An earlier SSE-triggered read may still be arriving. Bind the next
        // request after the failed reread, then inspect that exact response.
        const refreshed = b.waitForRequest(request => request.method() === "GET"
          && request.url().endsWith("/api/connections"));
        await b.evaluate(() => window.dispatchEvent(new Event("focus")));
        const response = await (await refreshed).response();
        expect(response.status()).toBe(200);
        const snapshot = await response.json();
        expect(snapshot.integration_application).toBe("not_applied");
        if (restarted) expect(snapshot.process_instance_id).not.toBe(prior.process_instance_id);
        else {
          expect(snapshot.process_instance_id).toBe(prior.process_instance_id);
          expect(snapshot.integration_revision).toBe(prior.integration_revision);
          expect(snapshot.connections_revision).toBeGreaterThan(prior.connections_revision);
        }
        const pause = b.getByText("配置未能一致生效，外部能力已暂停；请修复后重新读取 .env。", {exact:true});
        await expect(pause).toBeVisible();
        const settled = late === "409"
          ? b.waitForResponse(response => response.url().endsWith("/api/connections/probe"))
          : b.waitForEvent("requestfailed", request => request.url().endsWith("/api/connections"));
        deliver(); await settled;
        if (late === "409" && !restarted) await expect(b.getByText("mutation_in_flight", {exact:true})).toBeVisible();
        else await expect(b.getByText("mutation_in_flight", {exact:true})).not.toBeVisible();
        await expect(pause).toBeVisible();
        await expect(b.getByText("连接状态读取失败，请重试。", {exact:true})).not.toBeVisible();
        expect((await control(s)).requests).toEqual(restarted ? [] : ["/usage"]);
        await control(s, {fault_publication:false});
        await a.getByRole("button", {name:"重新读取 .env"}).click();
        await expect(card(a)).toContainText("已配置未测试");
        await b.evaluate(() => window.dispatchEvent(new Event("focus")));
        await expect(pause).not.toBeVisible();
        await expect(card(b)).toContainText("已配置未测试");
      } finally {deliver?.(); await control(s, {release:true}); try {await context.close();} finally {await s.close();}}
    });
  }
}

test('S08 STD02 connection details and operation focus survive refresh without stealing MainBar focus',async({browser})=>{
  const s=await server(),context=await browser.newContext();let release=()=>{};
  try {
    const page=await context.newPage(),origin=`http://127.0.0.1:${s.entry.port}`;await page.goto(origin+'/connections');
    const card=page.locator('[data-integration="tavily"]');await card.getByText('集成详情',{exact:true}).click();
    const link=card.getByRole('link',{name:'到工具页核对授权',exact:true});await link.focus();
    const reread=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/connections');
    await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await reread;await expect(link).toBeFocused();
    let entered,posts=0;let received=new Promise(resolve=>entered=resolve);let held=new Promise(resolve=>release=resolve);
    await page.route('**/api/connections/probe',async route=>{posts++;const response=await route.fetch();entered();await held;await route.fulfill({response}).catch(()=>{});});
    const button=card.getByRole('button',{name:'测试连接',exact:true});await button.click();await received;release();
    await expect(card).toContainText('连接：已连接');await expect(button).toBeFocused();
    received=new Promise(resolve=>entered=resolve);held=new Promise(resolve=>release=resolve);
    await button.click();await received;const chat=page.getByRole('button',{name:'新建会话',exact:true});await chat.focus();
    release();await expect(button).toBeEnabled();await expect(chat).toBeFocused();expect(posts).toBe(2);
    expect((await control(s)).requests).toEqual(['/usage']);
  }finally{release();await context.close();await s.close();}
});

test('S08 STD01 Connections already isolates retired probe cleanup from a successor',async({browser})=>{
  const s=await server(),context=await browser.newContext();let releaseA=()=>{},releaseB=()=>{};
  try {
    const page=await context.newPage(),origin=`http://127.0.0.1:${s.entry.port}`;await page.goto(origin+'/connections');
    const card=page.locator('[data-integration="tavily"]'),button=card.getByRole('button',{name:'测试连接',exact:true});
    let gotA,gotB,posts=0;const recvA=new Promise(resolve=>gotA=resolve),recvB=new Promise(resolve=>gotB=resolve);
    const heldA=new Promise(resolve=>releaseA=resolve),heldB=new Promise(resolve=>releaseB=resolve);
    await page.route('**/api/connections/probe',async route=>{
      const index=++posts,response=await route.fetch();expect(response.status()).toBe(200);
      if(index===1){gotA();await heldA;}else{gotB();await heldB;}
      await route.fulfill({response}).catch(()=>{});
    });
    await button.click();await recvA;await context.setOffline(true);
    await expect(page.getByText('连接中断，原操作结果未确认；请核对，不会自动重送。',{exact:true})).toBeVisible();
    await context.setOffline(false);await expect(button).toBeEnabled();await button.click();await recvB;
    const arrived=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/connections/probe');releaseA();await arrived;
    await expect(button).toBeDisabled();await expect(card).toContainText('测试连接正在提交');
    releaseB();await expect(button).toBeEnabled();expect(posts).toBe(2);
    expect((await control(s)).requests).toEqual(['/usage']);
  }finally{releaseA();releaseB();await context.setOffline(false);await context.close();await s.close();}
});
