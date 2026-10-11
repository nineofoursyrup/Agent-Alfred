async (page) => {
  const base='docs/implementation/dashboard-claude-design/evidence/V05';
  await page.clock.install({time:new Date('2026-10-11T02:15:00Z')});
  await page.setViewportSize({width:1440,height:900});
  async function read() {
    await page.goto('http://127.0.0.1:17920/inbox');
    await page.getByRole('region',{name:'会话列表',exact:true}).getByRole('link',{name:'创建工具测试日程',exact:true}).waitFor();
    return {snapshot:await page.getByRole('main').ariaSnapshot(),styles:await page.locator('.inbox-rows, .inbox-rows *').evaluateAll(nodes=>nodes.map(node=>{const s=getComputedStyle(node),r=node.getBoundingClientRect();return [node.tagName,node.className,node.textContent,s.fontSize,s.color,s.backgroundColor,s.borderRadius,r.width,r.height];}))};
  }
  const current = await read();
  await page.screenshot({path:`${base}/inbox-current-1440x900.png`});
  await page.route('**/assets/inbox-runs.css',route=>route.fulfill({path:'/Users/nineofour/Agent-Alfred-v05/output/playwright/v05-inbox-runs-css-baseline',contentType:'text/css'}));
  await page.route('**/assets/run-fields.js',route=>route.fulfill({path:'/Users/nineofour/Agent-Alfred-v05/output/playwright/v05-run-fields-js-baseline',contentType:'text/javascript'}));
  await page.route('**/assets/runs.js',route=>route.fulfill({path:'/Users/nineofour/Agent-Alfred-v05/output/playwright/v05-runs-js-baseline',contentType:'text/javascript'}));
  const baseline = await read();
  await page.screenshot({path:`${base}/inbox-baseline-1440x900.png`});
  await page.unroute('**/assets/inbox-runs.css');
  await page.unroute('**/assets/run-fields.js');
  await page.unroute('**/assets/runs.js');

  return {sameMainSnapshot:current.snapshot===baseline.snapshot,sameInboxComputedStyles:JSON.stringify(current.styles)===JSON.stringify(baseline.styles),current,baseline};
}
