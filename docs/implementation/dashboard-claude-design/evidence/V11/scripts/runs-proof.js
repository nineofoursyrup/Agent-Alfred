async (page) => {
  const base = '/Users/nineofour/Agent-Alfred-v11/.scratch/v11/capture';
  const origin = 'http://127.0.0.1:18110';
  const audit = {clock:'2026-10-12T01:15:00Z',server:'tests/browser/server.py',reduced_motion:'reduce',results:[]};
  await page.clock.setFixedTime(new Date(audit.clock));
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.setViewportSize({width:1440,height:900});
  await page.goto(origin + '/inbox?session_id=v11-session-1&view=messages');
  await page.getByRole('region',{name:'会话只读预览'}).locator('article').first().waitFor({state:'attached'});
  await page.getByRole('button',{name:'继续此会话',exact:true}).click();
  await page.waitForFunction(() => sessionStorage.getItem('alfred.session') === 'v11-session-1');
  async function read() {
    await page.goto(origin + '/runs');
    await page.getByRole('region',{name:'运行列表'}).locator('[data-source-id]').first().waitFor({state:'attached'});
    await page.waitForFunction(() => !document.querySelector('.runs-page .run-list-controls [role="status"]')?.textContent.includes('读取中'));
    await page.getByRole('region',{name:'主对话',includeHidden:true}).getByText('已保存',{exact:true}).first().waitFor({state:'attached'});
  }
  for (const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
    await page.setViewportSize({width,height});
    await read();
    const after = await page.getByRole('main').ariaSnapshot();
    await page.screenshot({path:`${base}/runs-${width}x${height}.png`,animations:'disabled'});
    await page.route('**/assets/inbox-runs.css',route => route.fulfill({path:'/Users/nineofour/Agent-Alfred-v11/.scratch/v11/baseline-inbox-runs.css',contentType:'text/css'}));
    await read();
    const before = await page.getByRole('main').ariaSnapshot();
    await page.screenshot({path:`${base}/runs-before-${width}x${height}.png`,animations:'disabled'});
    audit.results.push({width,height,same_accessibility:before===after});
    await page.unroute('**/assets/inbox-runs.css');
  }
  return audit;
}
