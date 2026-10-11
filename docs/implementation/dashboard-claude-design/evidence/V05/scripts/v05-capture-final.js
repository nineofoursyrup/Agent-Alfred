async (page) => {
  const base = 'docs/implementation/dashboard-claude-design/evidence/V05';
  const viewports = [[1440,900],[1280,800],[390,844],[320,800]];
  const audit = { snapshots: [], reads: [], console: [] };
  page.on('console', message => { if (['error','warning'].includes(message.type())) audit.console.push({type:message.type(),text:message.text()}); });
  const target = '0fea87cbb46c45b1afce86d9a1b1a9bc';
  await page.goto('http://127.0.0.1:17920/runs');
  const list = page.getByRole('region',{name:'运行列表',exact:true});
  await list.locator(`[data-source-id="${target}"]`).getByRole('link',{name:'查看运行',exact:true}).click();
  await page.getByRole('button',{name:'刷新过程证据',exact:true}).waitFor();
  await page.getByRole('link',{name:'返回来源',exact:true}).click();
  await list.locator('[data-selected="true"]').waitFor();
  for (const [width,height] of viewports) {
    await page.setViewportSize({width,height});
    await page.screenshot({path:`${base}/runs-${width}x${height}.png`});
    audit.snapshots.push({page:'list',width,height,overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),inlineStyles:await page.locator('.runs-page [style]').count(),accessibility:await page.getByRole('main').ariaSnapshot()});
  }
  page.on('request', request => { if (request.method()==='GET') audit.reads.push(new URL(request.url()).pathname); });
  await page.goto(`http://127.0.0.1:17920/runs/${target}`);
  const detail = page.getByRole('region',{name:'运行过程',exact:true});
  await detail.getByText('过程记录可用 · available',{exact:true}).waitFor();
  for (const [width,height] of viewports) {
    await page.setViewportSize({width,height});
    await page.screenshot({path:`${base}/detail-${width}x${height}.png`});
    audit.snapshots.push({page:'detail',width,height,overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),inlineStyles:await page.locator('.runs-page [style]').count(),accessibility:await page.getByRole('main').ariaSnapshot()});
  }
  audit.detailReads = [...audit.reads];
  await page.setViewportSize({width:1440,height:900});
  await page.goto('http://127.0.0.1:17920/inbox');
  await page.getByRole('region',{name:'会话列表',exact:true}).getByRole('link',{name:'创建工具测试日程',exact:true}).waitFor();
  await page.screenshot({path:`${base}/inbox-1440x900.png`});
  audit.inbox = await page.getByRole('main').ariaSnapshot();
  audit.target = target;
  return audit;
}
