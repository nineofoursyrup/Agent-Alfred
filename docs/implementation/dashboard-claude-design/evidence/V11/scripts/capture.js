async (page) => {
  const base = '/Users/nineofour/Agent-Alfred-v11/.scratch/v11/capture';
  const origin = 'http://127.0.0.1:18110';
  const views = [[1440,900],[1280,800],[390,844],[320,800]];
  const audit = {clock: '2026-10-12T01:15:00Z', server: 'tests/browser/server.py', responses: [], errors: [], request_failures: [], snapshots: [], runs_proof: []};
  page.on('pageerror', error => audit.errors.push(String(error)));
  page.on('requestfailed', request => audit.request_failures.push({url:request.url(),error:request.failure()?.errorText}));
  page.on('request', request => {if (new URL(request.url()).pathname.startsWith('/api/')) audit.responses.push({method:request.method(),path:new URL(request.url()).pathname});});
  await page.clock.setFixedTime(new Date(audit.clock));
  await page.setViewportSize({width:1440,height:900});
  await page.goto(origin + '/inbox?session_id=v11-session-1&view=messages');
  await page.getByRole('region', {name:'会话只读预览'}).locator('article').first().waitFor();
  await page.getByRole('button', {name:'继续此会话',exact:true}).click();
  await page.waitForFunction(() => sessionStorage.getItem('alfred.session') === 'v11-session-1');
  const selected = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
  async function capture(name, ready) {
    await ready();
    for (const [width, height] of views) {
      await page.setViewportSize({width,height});
      await page.screenshot({path:`${base}/${name}-${width}x${height}.png`, animations:'disabled'});
      audit.snapshots.push({name,width,height,selected_session:await page.evaluate(() => sessionStorage.getItem('alfred.session')),
        overflow:await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),inline_styles:await page.locator('.inbox-page [style]').count(),
        page_width:await page.locator('.page-body').evaluate(el => el.getBoundingClientRect().width),
        accessibility:await page.getByRole('main').ariaSnapshot(),
        rows:await page.locator('.sy-row').evaluateAll(elements => elements.map(el => {
          const style = getComputedStyle(el), selected = getComputedStyle(el,'::before');
          const meta = el.querySelector('.sy-row-meta');
          const metaStyle = meta ? getComputedStyle(meta) : null;
          return {key:el.getAttribute('data-source-id'), selected:el.getAttribute('data-selected'),current:el.getAttribute('aria-current'),
            text:el.textContent,font:style.fontSize,color:style.color,meta_font:metaStyle?.fontSize,meta_family:metaStyle?.fontFamily,
            selected_opacity:selected.opacity,selected_color:selected.backgroundColor,selected_shadow:selected.boxShadow};
        }))});
    }
  }
  await page.goto(origin + '/inbox');
  await capture('inbox', () => page.getByRole('region',{name:'会话列表'}).locator('article').first().waitFor());
  await page.goto(origin + '/inbox?session_id=v11-session-2&view=messages');
  const preview = page.getByRole('region', {name:'会话只读预览'});
  await preview.locator('article').first().waitFor();
  await preview.getByRole('link', {name:'查看运行',exact:true}).first().click();
  await page.getByRole('button', {name:'刷新过程证据',exact:true}).waitFor();
  await page.getByRole('link', {name:'返回来源',exact:true}).click();
  await capture('inbox-preview', () => preview.locator('[data-selected="true"]').waitFor());
  await page.goto(origin + '/inbox?session_id=v11-session-2&view=runs');
  await capture('inbox-runs', () => preview.locator('article').first().waitFor());
  if (await page.evaluate(() => sessionStorage.getItem('alfred.session')) !== selected) throw new Error('read_only_preview_changed_session');
  async function readRuns() {
    await page.goto(origin + '/runs');
    await page.getByRole('region',{name:'运行列表'}).locator('[data-source-id]').first().waitFor();
    await page.waitForFunction(() => !document.querySelector('.runs-page .run-list-controls [role="status"]')?.textContent.includes('读取中'));
  }
  for (const [width,height] of views) {
    await page.setViewportSize({width,height});
    await readRuns();
    const after = {snapshot:await page.getByRole('main').ariaSnapshot()};
    await page.screenshot({path:`${base}/runs-${width}x${height}.png`,animations:'disabled'});
    await page.route('**/assets/inbox-runs.css', route => route.fulfill({path:'/Users/nineofour/Agent-Alfred-v11/.scratch/v11/baseline-inbox-runs.css',contentType:'text/css'}));
    await readRuns();
    const before = {snapshot:await page.getByRole('main').ariaSnapshot()};
    await page.screenshot({path:`${base}/runs-before-${width}x${height}.png`,animations:'disabled'});
    audit.runs_proof.push({width,height,same_accessibility:before.snapshot===after.snapshot});
    await page.unroute('**/assets/inbox-runs.css');
  }
  return audit;
}
