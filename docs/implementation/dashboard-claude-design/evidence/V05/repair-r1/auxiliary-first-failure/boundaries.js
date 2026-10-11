async (page) => {
  const target = "0fea87cbb46c45b1afce86d9a1b1a9bc";
  const base = "docs/implementation/dashboard-claude-design/evidence/V05/repair-r1";
  const response = await page.request.get(`http://127.0.0.1:17920/api/runs/locate/${target}`);
  const original = (await response.json()).runs.find(run => run.run_id === target);
  const run = {...original, started_at:"2026-10-11T01:00:00Z", finished_at:"2026-10-11T01:00:00Z"};
  const event = (seq, attempt, name, payload = {}) => ({seq, envelope:{run_id:target, step_index:0, attempt_id:attempt, ts:seq}, payload:{name,...payload}});
  const accounts = [
    {attempt_id:"void",outcome:"aborted",usage:{output_tokens:3},cost:{state:"unknown",amount:"999"}},
    {attempt_id:"estimated",outcome:"committed",usage:{output_tokens:6},cost:{state:"estimated",amount:"0.006",price_components:[{dimension:"output",price_source:"catalog",stale:true},{dimension:"uncached_input",price_source:"model_static",tiered:true}]}},
    {attempt_id:"exact",outcome:"committed",usage:{output_tokens:9},cost:{state:"exact",amount:"0.125"}},
  ];
  const evidence = {run_id:target,trace_status:"available",trace_incomplete:true,recording_state:"recorded",attempts:accounts,events:[
    event(1,"void","attempt.started"),event(2,"void","attempt.aborted",{blocks:[{type:"text",text:"已作废片段"}]}),
    event(3,"estimated","attempt.started"),event(4,"estimated","attempt.committed",{blocks:[{type:"text",text:"提交快照"}]}),
    event(5,"exact","attempt.started"),event(6,"exact","attempt.committed",{blocks:[{type:"text",text:"账目核对快照"}]}),
    event(7,null,"run.finished",{duration_ms:137}),
  ]};
  const cases = ["trace-incomplete","no-reply","unplaced-accounts","empty-list","accounts-unread","accounts-failed","accounts-empty","accounts-expired","duration-fallback"];
  const report = {target,original,run,evidence,viewports:[[1440,900],[320,800]],snapshots:[],checks:[],console:[]};
  const check = (condition, name, width) => report.checks.push({name,width,pass:Boolean(condition)});
  for (const [width,height] of report.viewports) for (const name of cases) {
    const context = await page.context().browser().newContext({viewport:{width,height}});
    const current = await context.newPage();
    const reads = [];
    current.on("request", request => {if(request.method()==="GET") reads.push(new URL(request.url()).pathname)});
    current.on("console", message => {if(message.type()==="error") report.console.push({name,width,text:message.text()})});
    const selected = name === "duration-fallback" ? {...run,finished_at:"2026-10-11T01:00:03Z"} : run;
    await current.route("**/api/runs/locate/*", route => route.fulfill({json:{filter:"chat",runs:[selected],non_terminal:null,next_cursor:null}}));
    await current.route("**/api/runs?*", route => route.fulfill({json:{filter:"all",runs:name === "empty-list" ? [] : [selected],non_terminal:null,next_cursor:null}}));
    let release;
    const held = new Promise(resolve => release = resolve);
    await current.route("**/api/run-evidence?*", async route => {
      if(name === "accounts-unread") await held;
      if(name === "accounts-failed") return route.fulfill({status:503,json:{error:{code:"controlled_evidence_failure"}}});
      if(name === "accounts-expired") return route.fulfill({status:410,json:{error:{code:"snapshot_expired"}}});
      const body = name === "trace-incomplete" ? evidence : name === "no-reply" ? {...evidence,trace_incomplete:false,memory:{routing:{reply_disposition:"no_reply",route:"fallback",decision_reason:"受控未回复"}}} : name === "unplaced-accounts" ? {...evidence,trace_status:"unavailable",trace_incomplete:false,events:[]} : {...evidence,trace_status:"unavailable",trace_incomplete:false,events:[],attempts:[]};
      return route.fulfill({json:body});
    });
    let responseRead;
    if(name !== "empty-list") responseRead = current.waitForResponse(response => response.url().includes("/api/run-evidence?"));
    await current.goto(`http://127.0.0.1:17920/${name === "empty-list" ? "runs" : `runs/${target}`}`);
    const summary = current.getByRole("region",{name:"运行摘要",exact:true});
    const process = current.getByRole("region",{name:"运行过程",exact:true});
    const usage = current.getByRole("region",{name:"用量与费用",exact:true});
    let geometry = null, focusAfterRefresh = null;
    if(name === "empty-list") {
      const empty = current.getByRole("cell",{name:"此筛选下暂无运行。",exact:true});
      await empty.waitFor();
      geometry = await empty.evaluate(node => {const row=node.parentElement,table=node.closest("table"),s=getComputedStyle(row);return {cell:node.getBoundingClientRect().toJSON(),table:table.getBoundingClientRect().toJSON(),rowClass:row.className,paddingLeft:s.paddingLeft,paddingRight:s.paddingRight,borderRadius:s.borderRadius}});
      check(geometry.rowClass.includes("run-list-row") && geometry.rowClass.includes("sy-row") && parseFloat(geometry.paddingLeft)>0 && geometry.borderRadius==="0px", "empty row uses padded row recipe",width);
    } else {
      await summary.locator(".run-summary-costs").waitFor();
      if(name !== "accounts-unread") {
        await responseRead;
        await current.waitForFunction(() => [...document.querySelectorAll("button")].some(node => node.textContent === "刷新过程证据" && !node.disabled));
      }
      const costs = await summary.locator(".run-summary-costs").innerText();
      const summaryText = await summary.innerText();
      if(name.startsWith("accounts-")) {
        const expected = name === "accounts-expired" ? "账目快照已失效，尚未核验当前调用账目。" : "暂无可读调用账目；不代表未调用或免费。";
        check(costs === expected && !/unknown|exact|estimated/.test(costs), name+" distinguishes missing evidence from call cost state",width);
        check((await usage.innerText()).includes(expected),name+" agrees with usage wording",width);
      }
      if(name === "trace-incomplete") {
        const text = await process.innerText();
        check(text.includes("追踪不完整") && !text.includes("过程记录可用 · available"),"partial trace never also claims available",width);
        check(summaryText.includes("137 ms") && summaryText.includes("已读 1 · 总数未提供"),"real millisecond duration and observed Step count",width);
        check(costs.includes("unknown · 费用未知") && costs.includes("estimated · USD 0.006") && costs.includes("exact · USD 0.125") && costs.includes("output: catalog · stale") && costs.includes("按基础档估算") && !costs.includes("999"),"readable per-call costs retained",width);
      }
      if(name === "no-reply") {
        const link = summary.getByRole("link",{name:"查看所属会话",exact:true});
        await link.waitFor();
        geometry = await link.evaluate(node => ({insideReadControls:Boolean(node.closest(".run-read-controls.sy-panel-body")),link:node.getBoundingClientRect().toJSON(),panel:node.closest(".run-summary-panel").getBoundingClientRect().toJSON()}));
        await summary.getByRole("button",{name:"刷新运行摘要",exact:true}).focus();
        await current.keyboard.press("Tab");
        focusAfterRefresh = await link.evaluate(node => document.activeElement===node);
        check(geometry.insideReadControls && focusAfterRefresh,"no-reply link has panel padding and follows refresh in keyboard order",width);
      }
      if(name === "unplaced-accounts") {
        const heading = process.getByRole("heading",{name:"调用账目 · 过程顺序不可用",exact:true});
        await heading.waitFor();
        geometry = await heading.evaluate(node => ({padded:Boolean(node.parentElement.classList.contains("sy-panel-body")),heading:node.getBoundingClientRect().toJSON(),process:node.closest(".run-process").getBoundingClientRect().toJSON()}));
        check(geometry.padded && geometry.heading.x>geometry.process.x,"unplaced ledger has process-panel indentation",width);
        check((await process.innerText()).includes("Attempt exact") && (await process.innerText()).includes("output_tokens: 9") && (await process.innerText()).includes("exact · USD 0.125"),"unplaced ledger facts visible",width);
      }
      if(name === "duration-fallback") check(summaryText.includes("3 s") && !summaryText.includes("3.0 s"),"timestamp fallback has whole-second precision",width);
      if(name === "trace-incomplete" || name === "unplaced-accounts") await process.locator(".run-process").scrollIntoViewIfNeeded();
    }
    await current.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const file = `${name}-${width}x${height}.png`;
    await current.screenshot({path:`${base}/${file}`});
    const overflow = await current.evaluate(() => document.documentElement.scrollWidth>innerWidth);
    check(!overflow,"no horizontal overflow: "+name,width);
    if(name !== "empty-list") check(!reads.includes("/api/runs"),"detail has no Run list read: "+name,width);
    report.snapshots.push({name,width,height,file,overflow,geometry,focusAfterRefresh,reads,accessibility:await current.getByRole("main").ariaSnapshot(),summary:name === "empty-list" ? null : await summary.innerText(),process:name === "empty-list" ? null : await process.innerText()});
    if(name === "accounts-unread") {release();await responseRead;}
    await context.close();
  }
  const listContext = await page.context().browser().newContext({viewport:{width:1440,height:900}});
  const listPage = await listContext.newPage();
  await listPage.route("**/api/runs?*",route => route.fulfill({json:{filter:"all",runs:[run],non_terminal:null,next_cursor:null}}));
  await listPage.goto("http://127.0.0.1:17920/runs");
  const row=listPage.locator(".run-list-row");await row.waitFor();
  const listText=await row.innerText(), accessible=await row.ariaSnapshot();
  check(!listText.includes("Step 数") && listText.includes("<1 s"),"list omits unsupported Step count and subsecond timestamp precision",1440);
  check(await row.locator(".run-list-preview dt.sr-only").count()===1 && accessible.includes("输入预览"),"shared sr-only preserves preview accessible term",1440);
  report.listBoundary={text:listText,accessibility:accessible};
  await listContext.close();
  report.ok=report.checks.every(item=>item.pass);
  return report;
}
