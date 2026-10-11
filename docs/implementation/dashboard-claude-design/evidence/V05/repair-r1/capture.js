async (page) => {
  const base = "docs/implementation/dashboard-claude-design/evidence/V05";
  const target = "0fea87cbb46c45b1afce86d9a1b1a9bc";
  const snapshots=[],processScreens=[],reads=[],console=[];
  for (const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
    const context=await page.context().browser().newContext({viewport:{width:1440,height:900}});
    const current=await context.newPage();
    current.on("console",message=>{if(["error","warning"].includes(message.type()))console.push({width,type:message.type(),text:message.text()})});
    await current.goto(`http://127.0.0.1:17920/runs/${target}`);
    const detail=current.getByRole("region",{name:"运行过程",exact:true});
    await detail.getByText("过程记录可用 · available",{exact:true}).waitFor();
    await current.locator(".run-actions").getByRole("button").click();
    await current.getByRole("region",{name:"主对话",exact:true}).getByText("已保存",{exact:true}).last().waitFor();
    await current.getByRole("link",{name:"返回运行列表",exact:true}).click();
    const list=current.getByRole("region",{name:"运行列表",exact:true});
    await list.locator(`[data-source-id="${target}"]`).getByRole("link",{name:"查看运行",exact:true}).click();
    await detail.getByText("过程记录可用 · available",{exact:true}).waitFor();
    await current.getByRole("link",{name:"返回来源",exact:true}).click();
    await list.locator('[data-selected="true"]').waitFor();
    await current.setViewportSize({width,height});
    await current.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    await current.screenshot({path:`${base}/runs-${width}x${height}.png`});
    snapshots.push({page:"list",width,height,overflow:await current.evaluate(()=>document.documentElement.scrollWidth>innerWidth),inlineStyles:await current.locator(".runs-page [style]").count(),accessibility:await current.getByRole("main").ariaSnapshot(),stepLabels:await list.locator("dt").allTextContents(),geometry:await current.evaluate(()=>({viewport:{width:innerWidth,height:innerHeight},page:document.getElementById("page").getBoundingClientRect().toJSON()}))});
    current.on("request",request=>{if(request.method()==="GET")reads.push({width,path:new URL(request.url()).pathname})});
    await current.goto(`http://127.0.0.1:17920/runs/${target}`);
    await detail.getByText("过程记录可用 · available",{exact:true}).waitFor();
    await current.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    await current.screenshot({path:`${base}/detail-${width}x${height}.png`});
    snapshots.push({page:"detail",width,height,overflow:await current.evaluate(()=>document.documentElement.scrollWidth>innerWidth),inlineStyles:await current.locator(".runs-page [style]").count(),accessibility:await current.getByRole("main").ariaSnapshot(),summary:await current.getByRole("region",{name:"运行摘要",exact:true}).innerText(),geometry:await current.evaluate(()=>({viewport:{width:innerWidth,height:innerHeight},page:document.getElementById("page").getBoundingClientRect().toJSON()}))});
    if(width<=390){
      await detail.locator(".run-process").scrollIntoViewIfNeeded();
      await current.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      await current.screenshot({path:`${base}/process-${width}x${height}.png`});
      processScreens.push({width,height,overflow:await current.evaluate(()=>document.documentElement.scrollWidth>innerWidth),scroll:await current.locator("#page").evaluate(node=>({top:node.scrollTop,height:node.scrollHeight,client:node.clientHeight})),accessibility:await current.getByRole("main").ariaSnapshot()});
    }
    await context.close();
  }
  const context=await page.context().browser().newContext({viewport:{width:1440,height:900}}),current=await context.newPage();
  await current.goto("http://127.0.0.1:17920/inbox");
  await current.getByRole("region",{name:"会话列表",exact:true}).getByRole("link",{name:"创建工具测试日程",exact:true}).waitFor();
  await current.screenshot({path:`${base}/inbox-1440x900.png`});
  const inbox=await current.getByRole("main").ariaSnapshot();await context.close();
  return {snapshots,processScreens,reads,console,inbox,target,context:"independent fresh Playwright context per viewport, no Clock installed",ok:snapshots.every(item=>!item.overflow&&item.inlineStyles===0&&(!item.stepLabels||!item.stepLabels.includes("Step 数")))&&processScreens.every(item=>!item.overflow)&&!reads.some(item=>item.path==="/api/runs")&&console.length===0};
}
