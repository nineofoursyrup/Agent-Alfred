import {test, expect} from '@playwright/test';
import {memoryServer} from './memory-server.js';

test('S09 mixed Attempt accounting exposes four price dimensions and freezes prices until explicit refresh', async ({page}) => {
  const server=await memoryServer({script:'tests/browser/ops_migration_server.py'});
  try {
    await server.send('mixed');
    await page.goto(server.origin+'/ops?range=all&timezone=UTC');
    const summary=page.getByRole('region',{name:'账目汇总',exact:true});
    await expect(summary).toContainText('精确费用 USD 1.25');
    await expect(summary).toContainText('估算费用 USD 0.00745');
    await expect(summary).toContainText('费用未知 Attempt 2');
    await page.getByRole('button',{name:/查看账目 /}).click();
    const attempt=page.getByRole('table',{name:'模型 Attempt'});
    await expect(attempt).toContainText('four-dim-aborted');
    await expect(attempt).toContainText('aborted');
    await page.getByText('Token 与四维价格：four-dim-aborted',{exact:true}).click();
    const prices=page.getByRole('table',{name:'四维价格 four-dim-aborted'});
    await expect(prices.getByRole('row')).toHaveCount(5);
    await expect(prices).toContainText('catalog');
    await expect(prices).toContainText('2026-10-01T00:00:00Z');
    await expect(prices).toContainText('价格已陈旧');
    await expect(prices).toContainText('阶梯价');
    const frozen=await prices.textContent();
    await server.send('price');
    await expect(prices).toHaveText(frozen);
    await expect(summary).toContainText('估算费用 USD 0.00745');
    await page.getByRole('button',{name:'刷新账目',exact:true}).click();
    await expect(summary).toContainText('估算费用 USD 0.0745');
    await expect(page.getByLabel('运行账目明细',{exact:true})).toBeEmpty();
  } finally {await server.close();}
});

test('S09 manual history is one UTF-8 segment, isolated from financial expiry and retired on Run switch', async ({page}) => {
  const server=await memoryServer({script:'tests/browser/ops_migration_server.py'});
  let release,detailRelease=()=>{};const gate=new Promise(resolve=>{release=resolve;});
  try {
    await server.send('long');
    await page.goto(server.origin+'/ops?range=all&timezone=UTC');
    await page.getByRole('button',{name:/查看账目 /}).click();
    const detail=page.getByRole('region',{name:'运行账目明细'});
    await expect(detail).toContainText('服务 fixture-service · 0.125 credits · 来源 receipt');
    let historyReads=0;
    page.on('request',request=>{if(new URL(request.url()).pathname==='/api/tools/history')historyReads++;});
    expect(historyReads).toBe(0);
    const first=page.waitForResponse(response=>response.url().includes('/api/tools/history?'));
    await page.getByRole('button',{name:'完整脱敏审计',exact:true}).click();
    const segment=await (await first).json(),preview=page.getByLabel('历史正文当前段');
    await expect(preview).toHaveText(segment.text);
    expect(segment.start).toBe(0);expect(segment.end).toBeLessThanOrEqual(256*1024);expect(segment.total_bytes).toBeGreaterThan(segment.end);
    expect(await page.evaluate(()=>window.historyInjected)).toBeUndefined();
    expect(historyReads).toBe(1);
    const second=page.waitForResponse(response=>response.url().includes('/api/tools/history?'));
    await page.getByRole('button',{name:'下一段',exact:true}).click();
    const next=await (await second).json();await expect(preview).toHaveText(next.text);
    expect(next.start).toBe(segment.end);expect(historyReads).toBe(2);
    await server.send('expire');
    await page.getByRole('button',{name:/查看账目 /}).click();
    await expect(page.getByText(/旧快照已失效/)).toBeVisible();
    await expect(detail).toContainText('服务 fixture-service');
    await page.getByRole('button',{name:'核验原操作当前结果',exact:true}).click();
    await expect(page.getByRole('region',{name:'当前核验',exact:true})).toContainText('读取时刻');
    await page.getByRole('button',{name:'完整脱敏审计',exact:true}).click();
    await expect(preview).toContainText('<script>');
    await server.send('simple');
    await page.getByRole('button',{name:'刷新账目',exact:true}).click();
    const rows=page.getByRole('button',{name:/查看账目 /});await expect(rows).toHaveCount(2);
    await rows.last().click();
    await expect(page.getByRole('button',{name:'完整脱敏审计',exact:true})).toBeVisible();
    let captured;const capturedRead=new Promise(resolve=>{captured=resolve;});
    await page.route('**/api/tools/history?**',async route=>{const response=await route.fetch();captured();await gate;await route.fulfill({response});});
    await page.getByRole('button',{name:'完整脱敏审计',exact:true}).click();await capturedRead;
    const detailGate=new Promise(resolve=>{detailRelease=resolve;});
    await page.route('**/api/ops/detail?**',async route=>{const response=await route.fetch();await detailGate;await route.fulfill({response});});
    await rows.first().click();
    // A new selection owns the detail immediately, before its response arrives.
    await expect(detail.getByRole('button',{name:'完整脱敏审计',exact:true})).toHaveCount(0);
    release();detailRelease();
    await expect(detail.getByRole('heading',{level:2})).toContainText((await rows.first().textContent()).replace('查看账目 ',''));
    await expect(preview).toBeEmpty();
  } finally {release();detailRelease();await server.close();}
});

test('S09 fixed range stays distinct from a changed or rejected filter draft', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/ops_server.py'});
  try {
    await server.send('bulk');
    await page.goto(server.origin+'/ops?range=all&timezone=UTC');
    const scope=page.getByRole('region',{name:'当前账目快照'});
    await expect(scope).toContainText('全部历史');
    const original=await scope.textContent();
    let posts=0;
    page.on('request',request=>{if(request.url().endsWith('/api/ops/snapshots'))posts++;});
    await page.getByLabel('IANA 时区',{exact:true}).fill('Invalid/Zone');
    await page.getByLabel('工具身份',{exact:true}).fill('draft-only');
    await expect(page.getByRole('status').filter({hasText:'筛选草稿已修改'})).toBeVisible();
    expect(posts).toBe(0);
    await expect(scope).toHaveText(original);
    await page.getByRole('button',{name:'刷新账目',exact:true}).click();
    await expect(page.getByRole('status').filter({hasText:'invalid_timezone'})).toBeVisible();
    expect(posts).toBe(1);
    await expect(scope).toHaveText(original);
    await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(50);
    await page.getByRole('button',{name:'下一页',exact:true}).click();
    await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(55);
    await page.getByRole('button',{name:/查看账目 /}).last().click();
    const href=await page.getByRole('link',{name:'进入运行过程（保留账目快照）'}).getAttribute('href');
    const context=new URL(href,server.origin).searchParams;
    expect(context.get('ops_range')).toBe('all');
    expect(context.get('ops_timezone')).toBe('UTC');
    expect(context.has('ops_tool')).toBe(false);
    await expect(page.getByRole('region',{name:'运行账目明细'})).not.toContainText('已持久记录');
  } finally {await server.close();}
});

test('S09 native return reuses the source snapshot and one bounded page, expiry requires explicit refresh', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/ops_server.py'});
  try {
    await server.send('bulk');
    await page.goto(server.origin+'/ops?range=all&timezone=Asia%2FShanghai');
    await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(50);
    await page.getByRole('button',{name:'下一页',exact:true}).click();
    await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(55);
    await page.getByRole('button',{name:/查看账目 /}).last().click();
    const entry=page.getByRole('link',{name:'进入运行过程（保留账目快照）'});
    const sourceURL=new URL(await entry.getAttribute('href'),server.origin);
    const snapshot=sourceURL.searchParams.get('snapshot_id');
    let posts=0; const pages=[];
    page.on('request',request=>{const url=new URL(request.url());if(url.pathname==='/api/ops/snapshots')posts++;if(url.pathname==='/api/ops')pages.push(url);});
    await entry.click();
    await expect(page).toHaveURL(sourceURL.href);
    await page.goBack();
    await expect(page.getByRole('region',{name:'当前账目快照'})).toContainText(snapshot);
    await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(5);
    await expect(page.getByRole('region',{name:'运行账目明细'})).toContainText(decodeURIComponent(sourceURL.pathname.slice(6)));
    expect(posts).toBe(0);
    expect(pages).toHaveLength(1);
    expect(pages[0].searchParams.get('offset')).toBe('50');
    expect(pages[0].searchParams.get('snapshot_id')).toBe(snapshot);
    await expect(entry).toBeFocused();
    await entry.click();
    await server.send('expire');
    await page.goBack();
    await expect(page.getByRole('status').filter({hasText:'原账目快照已失效'})).toBeVisible();
    await expect(page.getByLabel('时间范围',{exact:true})).toHaveValue('all');
    await expect(page.getByLabel('IANA 时区',{exact:true})).toHaveValue('Asia/Shanghai');
    expect(posts).toBe(0);
    await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(0);
    await page.getByRole('button',{name:'刷新账目',exact:true}).click();
    await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(50);
    expect(posts).toBe(1);
    await expect(page.getByRole('region',{name:'当前账目快照'})).not.toContainText(snapshot);
  } finally {await server.close();}
});

test('S09 direct source reload and read retry never create a replacement snapshot', async ({page,context}) => {
  const server=await memoryServer({script:'tests/browser/ops_server.py'});
  let release=()=>{};
  try {
    await page.goto(server.origin+'/ops?range=all&timezone=UTC');
    const identity=await page.getByRole('region',{name:'当前账目快照'}).getByText(/snapshot_id /).textContent();
    const snapshot=identity.match(/snapshot_id (\S+)/)[1];
    let posts=0,reads=0;
    page.on('request',request=>{const path=new URL(request.url()).pathname;if(path==='/api/ops/snapshots')posts++;if(path==='/api/ops')reads++;});
    await page.route('**/api/ops?**',route=>route.abort('failed'));
    await page.goto(server.origin+'/ops?range=all&timezone=UTC&snapshot_id='+snapshot);
    await expect(page.getByText(/原快照核验失败/)).toBeVisible();
    expect(posts).toBe(0);expect(reads).toBe(1);
    await page.unroute('**/api/ops?**');
    await page.getByRole('button',{name:'重新核验原快照',exact:true}).click();
    await expect(page.getByRole('region',{name:'当前账目快照'})).toContainText(snapshot);
    expect(posts).toBe(0);expect(reads).toBe(2);
    await page.reload();
    await expect(page.getByRole('region',{name:'当前账目快照'})).toContainText(snapshot);
    expect(posts).toBe(0);expect(reads).toBe(3);
    let captured;const capturedRead=new Promise(resolve=>{captured=resolve;});const gate=new Promise(resolve=>{release=resolve;});
    await page.route('**/api/ops?**',async route=>{const response=await route.fetch();captured();await gate;await route.fulfill({response}).catch(error=>{if(!String(error).includes('Route is already handled'))throw error;});});
    await page.reload();await capturedRead;
    await context.setOffline(true);await expect(page.getByText(/连接中断；财务内容/)).toBeVisible();
    release();await page.unroute('**/api/ops?**');await context.setOffline(false);
    const retry=page.getByRole('button',{name:'重新核验原快照',exact:true});
    await expect(retry).toBeEnabled();await retry.click();
    await expect(page.getByRole('region',{name:'当前账目快照'})).toContainText(snapshot);
    expect(posts).toBe(0);
  } finally {release();try{await context.setOffline(false);}finally{await server.close();}}
});

test('S09 a new process retires a held financial response and clears body without replacing the old ledger',async({page})=>{
  const server=await memoryServer({script:'tests/browser/ops_migration_server.py'});
  let release;const gate=new Promise(resolve=>{release=resolve;});
  try{
    await server.send('long');
    await page.goto(server.origin+'/ops?range=all&timezone=UTC');
    await page.getByRole('button',{name:/查看账目 /}).click();
    await page.getByRole('button',{name:'完整脱敏审计',exact:true}).click();
    const preview=page.getByLabel('历史正文当前段');await expect(preview).toContainText('<script>');
    const scope=page.getByRole('region',{name:'当前账目快照'}),fixed=await scope.textContent();
    let captured;const persisted=new Promise(resolve=>{captured=resolve;});
    await page.route('**/api/ops/snapshots',async route=>{const response=await route.fetch();captured();await gate;await route.fulfill({response});});
    await page.getByRole('button',{name:'刷新账目',exact:true}).click();await persisted;
    await server.restart();
    await expect(page.getByText(/旧快照已失效/)).toBeVisible();
    await expect(preview).toBeEmpty();
    await expect(page.getByText(/实例已变化；历史正文需重新核验/)).toBeVisible();
    release();
    await expect(scope).toHaveText(fixed);
    await expect(page.getByRole('button',{name:/查看账目 /})).toBeDisabled();
    await expect(page.getByRole('button',{name:'刷新账目',exact:true})).toBeEnabled();
    await page.getByRole('button',{name:'核验原操作当前结果',exact:true}).click();
    await expect(page.getByRole('region',{name:'当前核验',exact:true})).toContainText('读取时刻');
    await page.getByRole('button',{name:'完整脱敏审计',exact:true}).click();
    await expect(preview).toContainText('<script>');
    await expect(scope).toHaveText(fixed);
  }finally{release();await server.close();}
});
