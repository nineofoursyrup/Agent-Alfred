import {test, expect} from '@playwright/test';
import {writeFile} from 'node:fs/promises';

const run = page => page.getByRole('button', {name:'执行', exact:true});
const result = page => page.getByRole('region', {name:'查询结果', exact:true});
const editor = page => page.getByRole('textbox', {name:'SQL', exact:true});
async function open(page) {
  await page.goto('/database');
  await expect(page.getByText('可执行', {exact:true})).toBeVisible();
}

// Check the API-to-view boundary, including fields that the previous catalog
// omitted, without creating a second source of production catalog definitions.
test('Database catalog preserves every approved field and expanded state across capability checks', async ({page}) => {
  await open(page);
  const catalog = await (await page.request.get('/api/database')).json();
  await expect(page.locator('.db-catalog > details').first()).not.toHaveAttribute('open');
  await page.getByText('对象目录', {exact:true}).click();
  const details = page.locator('.db-catalog details[data-object]');
  expect(catalog.objects).toHaveLength(21);
  await expect(details).toHaveCount(21);
  for (const object of catalog.objects) {
    const item = page.locator(`[data-object="${object.name}"]`);
    await item.locator('summary').click();
    await expect(item.locator('dt')).toHaveText(object.columns.map(column => column.name));
    await expect(item.locator('dd')).toHaveText(object.columns.map(column =>
      `${column.type} · ${column.nullable ? '可空' : 'NOT NULL'} · ${column.identity ? '身份字段' : '非身份字段'}；来源 ${column.origin}；${column.protection}`));
    await expect(item.locator('pre')).toHaveText(object.example);
    await expect(item).toContainText(object.source);
    if (object.notes) await expect(item).toContainText(object.notes);
  }
  await page.getByText('完整限制与寿命', {exact:true}).click();
  const limits = page.locator('.db-catalog > details').last();
  await expect(limits).toContainText('64 KiB');
  await expect(limits).toContainText('64 MiB');
  await expect(limits).toContainText('128 MiB');
  await expect(limits).toContainText('2 MiB');
  await expect(limits).toContainText('各 1 秒预算');
  await expect(limits).toContainText('未执行 30 秒到期；终态保留 60 秒；容量 64');
  await expect(limits).toContainText('句柄期限不是结果 TTL');
  await page.getByRole('button', {name:'核验查询与可用性',exact:true}).click();
  await expect(page.getByText('可执行', {exact:true})).toBeVisible();
  await expect(page.locator('.db-catalog details[data-object][open]')).toHaveCount(21);
  await expect(limits).toHaveAttribute('open');
});

for (const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
  test.describe(`${width}x${height}`, () => {
  test.use({viewport:{width,height},hasTouch:width<1100,deviceScaleFactor:1});
  test(`Database types, long content and keyboard actions remain reachable at ${width}x${height}`, async ({page}, testInfo) => {
    await page.setViewportSize({width,height});
    await page.emulateMedia({reducedMotion:'reduce'});
    await open(page);
    const long = '长文本 complete-value-'.repeat(40) + 'LAST-VALUE';
    await editor(page).fill(`SELECT NULL AS v, '' AS v, 1 AS v, 1.0 AS v, X'01ff' AS v, '${long}' AS long_value`);
    if (width<1100) await run(page).tap(); else await run(page).click();
    await expect(result(page).locator('th')).toHaveText(['v','v','v','v','v','long_value']);
    await expect(result(page).locator('td')).toHaveText(['NULL','""','1','1.0','BLOB 01ff (2 字节)',JSON.stringify(long)]);
    const scroll = page.getByRole('region', {name:'数据表横向滚动区',exact:true});
    await scroll.focus();
    await expect(scroll).toBeFocused();
    await page.keyboard.press('ArrowRight');
    await expect.poll(() => scroll.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
    const cell = page.getByRole('region', {name:'完整单元格值',exact:true});
    await cell.focus();
    await expect(cell).toBeFocused();
    await expect(cell).toContainText('LAST-VALUE');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator('#page').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    for (const button of [run(page), page.getByRole('button',{name:'取消',exact:true}),
      page.getByRole('button',{name:'核验查询与可用性',exact:true}),
      page.getByRole('button',{name:'复制 SQL',exact:true}),
      page.getByRole('button',{name:'复制本页结果',exact:true})]) {
      await button.scrollIntoViewIfNeeded();
      const box = await button.boundingBox();
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x+box.width).toBeLessThanOrEqual(width);
    }
    if (width < 1100) {
      await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
      await expect(editor(page)).not.toBeVisible();
      await expect(page.locator('[aria-label="查询结果"] td').last()).toHaveText(JSON.stringify(long));
      await page.getByRole('button',{name:'收起主对话',exact:true}).click();
      await expect(editor(page)).toHaveValue(/LAST-VALUE/);
    }
    const metrics = await page.evaluate(() => {
      const style = selector => {
        const computed = getComputedStyle(document.querySelector(selector));
        return {fontSize:computed.fontSize,color:computed.color,background:computed.backgroundColor};
      };
      return {viewport:[innerWidth,innerHeight], dpr:devicePixelRatio,
        central:document.querySelector('.page-body').getBoundingClientRect().width,
        body:style('.db-console'), table:style('.db-console table'), meta:style('.db-result-meta'),
        activeAnimations:document.getAnimations().length, browser:navigator.userAgent,
        maxTouchPoints:navigator.maxTouchPoints,
        scope:innerWidth<1100?'Chromium viewport simulation, no real mobile keyboard':'Chromium desktop viewport'};
    });
    expect(metrics.maxTouchPoints > 0).toBe(width < 1100);
    expect(metrics.body.fontSize).toBe('14px');
    expect(metrics.table.fontSize).toBe('13px');
    expect(metrics.meta.fontSize).toBe('12px');
    expect(metrics.activeAnimations).toBe(0);
    const metricFile = testInfo.outputPath('geometry-and-environment.json');
    await writeFile(metricFile,JSON.stringify(metrics,null,2));
    await testInfo.attach('geometry-and-environment', {path:metricFile,contentType:'application/json'});
    await page.locator('#page').evaluate(el => el.scrollTop=0);
    await page.screenshot({path:testInfo.outputPath(`database-${width}x${height}-top.png`)});
    await result(page).scrollIntoViewIfNeeded();
    await page.screenshot({path:testInfo.outputPath(`database-${width}x${height}-long-result.png`)});
  });
  });
}

test('Database row, column and byte results follow actual worker boundaries', async ({page},testInfo) => {
  await open(page);
  await editor(page).fill('SELECT '+Array.from({length:64},(_,i)=>`${i} AS col_${i}`).join(','));
  await run(page).click();
  await expect(result(page).locator('th')).toHaveCount(64);
  await expect(result(page).locator('th').last()).toHaveText('col_63');
  await editor(page).fill('SELECT '+Array.from({length:65},(_,i)=>i).join(','));
  const rejected = page.waitForResponse(r=>r.url().endsWith('/execute'));
  await run(page).click();
  expect((await rejected).status()).toBe(400);
  await expect(page.locator('[data-state]')).toContainText('请求被拒绝');
  await expect(result(page)).toBeEmpty();
  await page.screenshot({path:testInfo.outputPath('database-65-columns-rejected.png')});
  const values = Array.from({length:11},(_,i)=>`SELECT ${i} AS n`).join(' UNION ALL ');
  const long = 'x'.repeat(50000)+'BYTE-END';
  await editor(page).fill(`WITH digits AS (${values}) SELECT '${long}' AS body FROM digits a CROSS JOIN digits b`);
  const completed = page.waitForResponse(r=>r.url().endsWith('/execute'));
  await run(page).click();
  const response = await completed;
  const body = await response.json();
  expect(response.status()).toBe(200);
  expect(body.truncation_reasons).toEqual(['bytes']);
  expect(body.returned_rows).toBeLessThan(121);
  expect(Buffer.byteLength(await response.text())).toBeLessThanOrEqual(2097152);
  await expect(page.locator('[data-state]')).toHaveText('结果截断 · 字节上限');
  await expect(result(page)).toContainText('字节上限 (bytes)');
  await expect(result(page).locator('td').first()).toHaveText(JSON.stringify(long));
});

test('Database failed clipboard writes give local feedback and unavailable results cannot copy', async ({page}) => {
  await open(page);
  await expect(page.getByRole('button',{name:'复制本页结果',exact:true})).toBeDisabled();
  await editor(page).fill('SELECT 1');
  // Clipboard is an external OS permission boundary. The query path remains real.
  await page.evaluate(() => navigator.clipboard.writeText = async () => {throw new DOMException('denied','NotAllowedError');});
  await page.getByRole('button',{name:'复制 SQL',exact:true}).click();
  await expect(page.getByText('复制失败；请检查剪贴板权限后重试。',{exact:true})).toBeVisible();
});

test('Database measures central 679/680 and shell 1099/1100 without clipping actions', async ({page},testInfo) => {
  await open(page);
  const evidence = [];
  for (const target of [679,680]) {
    await page.setViewportSize({width:target+32,height:800});
    const actual = await page.locator('.page-body').evaluate(el=>el.getBoundingClientRect().width);
    await page.setViewportSize({width:target+32+(target-actual),height:800});
    const central = await page.locator('.page-body').evaluate(el=>el.getBoundingClientRect().width);
    expect(central).toBe(target);
    const columns = await page.locator('.db-facts').evaluate(el=>getComputedStyle(el).gridTemplateColumns.split(' ').length);
    expect(columns).toBe(target<680?1:3);
    evidence.push({target,central,columns,viewport:page.viewportSize()});
    await page.screenshot({path:testInfo.outputPath(`database-central-${target}.png`)});
  }
  for (const width of [1099,1100]) {
    await page.setViewportSize({width,height:800});
    const geometry = await page.evaluate(()=>({viewport:innerWidth,
      central:document.querySelector('.page-body').getBoundingClientRect().width,
      rail:document.querySelector('.site-header').getBoundingClientRect().width,
      mainbar:document.querySelector('#mainbar').getBoundingClientRect().width,
      clipped:document.documentElement.scrollWidth>innerWidth}));
    expect(geometry.clipped).toBe(false);
    if(width===1100){expect(geometry.rail).toBe(232);expect(geometry.mainbar).toBe(320);}
    evidence.push(geometry);
    await page.screenshot({path:testInfo.outputPath(`database-shell-${width}.png`)});
  }
  const metricFile = testInfo.outputPath('actual-breakpoints.json');
  await writeFile(metricFile,JSON.stringify(evidence,null,2));
  await testInfo.attach('actual-breakpoints', {path:metricFile,contentType:'application/json'});
});
