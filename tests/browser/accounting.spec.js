import {expect} from '@playwright/test';
import {test, createChatSession, sendChat} from './chat-fixture.js';

test('A01 A02 A23 A28: MainBar to real Tools and immutable Ops ledger', async ({page, chatServer}) => {
  await page.goto(chatServer.origin + '/inbox');
  await createChatSession(page);
  await page.getByRole('button', {name:'展开对话', exact:true}).click();
  await sendChat(page, chatServer, '创建工具测试日程');
  await expect(page.getByRole('region', {name:'主对话'})).toContainText('已查询到工具测试日程');
  await page.getByRole('link', {name:'Tools', exact:true}).click();
  const tool = page.locator('article').filter({has:page.getByRole('heading',{name:'create_event',exact:true})});
  await expect(tool).toContainText('不需要外部授权');
  await expect(tool.locator('select')).toHaveCount(0);
  await tool.getByRole('link',{name:'查看包含该工具的运行'}).click();
  await expect(page.getByRole('heading',{name:'Ops 账本'})).toBeVisible();
  await expect(page.getByLabel('账目汇总')).toContainText('工具请求');
  await page.getByRole('button',{name:/查看账目 /}).first().click();
  const detail = page.getByLabel('运行账目明细');
  await expect(detail).toContainText('已确认启动');
  await expect(detail).toContainText('query_events');
  await detail.getByRole('button',{name:'展开模型结果投影'}).first().click();
  await expect(page.getByLabel('历史正文当前段')).toContainText('created');
  await detail.getByRole('button',{name:'模型提交参数（脱敏）'}).first().click();
  await expect(page.getByLabel('历史正文当前段')).toContainText('title');
  await page.getByRole('link',{name:'Tools',exact:true}).click();
  await expect(page.getByLabel('历史正文当前段')).toHaveCount(0);
});

import {memoryServer} from './memory-server.js';

test('A23: refresh cannot accept another page from the previous snapshot', async ({page}) => {
  const server = await fixture();
  let release; const gate = new Promise(resolve => {release=resolve;});
  try {
    await server.send('bulk'); await page.goto(server.origin+'/ops');
    await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(50);
    await page.route('**/api/ops/snapshots',async route => {const response=await route.fetch(); await gate; await route.fulfill({response});});
    await page.getByRole('button',{name:'刷新账目'}).click();
    await expect(page.getByRole('button',{name:'下一页',exact:true})).toBeDisabled();
    release();
    await expect(page.getByText('固定账目快照；后台变化后请显式刷新。')).toBeVisible();
    await page.getByRole('button',{name:'下一页',exact:true}).click();
    await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(55);
  } finally {release(); await server.close();}
});

test('A06: a late successful authorization receipt does not replace newer pending state', async ({page,context}) => {
  const server=await fixture(), other=await context.newPage();
  let release, saved; const gate=new Promise(resolve=>{release=resolve;});
  const persisted=new Promise(resolve=>{saved=resolve;});
  try {
    await page.goto(server.origin+'/tools');
    await page.route('**/api/tools/authorization',async route=>{const response=await route.fetch(); saved(); await gate; await route.fulfill({response});});
    await externalCard(page).getByLabel('external_fixture 授权草稿').selectOption('allowed');
    await externalCard(page).getByRole('button',{name:'保存授权',exact:true}).click(); await persisted;
    await other.goto(server.origin+'/tools');
    await server.send('apply-fail');
    await externalCard(other).getByLabel('external_fixture 授权草稿').selectOption('denied');
    await externalCard(other).getByRole('button',{name:'保存授权',exact:true}).click();
    await expect(other.getByText(/生效：pending/)).toBeVisible(); release();
    await expect(page.getByText(/生效：pending/)).toBeVisible();
    await expect(page.getByText(/授权已生效/)).toHaveCount(0);
    await expect(externalCard(page).getByLabel('external_fixture 授权草稿')).toHaveValue('allowed');
  } finally {release(); await other.close(); await server.close();}
});

async function fixture() {return memoryServer({script:'tests/browser/ops_server.py'});}
const externalCard = page => page.locator('article').filter({has:page.getByRole('heading',{name:'external_fixture',exact:true})});

test('A04 A05 A06 A08: two tabs keep drafts, reject busy, and distinguish lost receipt from application', async ({page,context}) => {
  const server=await fixture(); const other=await context.newPage();
  try {
    await page.goto(server.origin+'/tools'); await other.goto(server.origin+'/tools');
    const card=externalCard(page), second=externalCard(other);
    await card.getByLabel('external_fixture 授权草稿').selectOption('allowed');
    await second.getByLabel('external_fixture 授权草稿').selectOption('denied');
    await server.send('busy');
    await card.getByRole('button',{name:'保存授权',exact:true}).click();
    await expect(page.getByText(/保存未完成：mutation_in_flight/)).toBeVisible();
    await server.send('idle');
    await card.getByRole('button',{name:'保存授权',exact:true}).click();
    await expect(card).toContainText('服务端当前保存值：allowed');
    await other.getByRole('button',{name:'核对当前授权'}).click();
    await expect(second).toContainText('草稿基于版本 0');
    await second.getByRole('button',{name:'保存授权',exact:true}).click();
    await expect(other.getByText(/authorization_conflict/)).toBeVisible();
    await expect(second.getByLabel('external_fixture 授权草稿')).toHaveValue('denied');
    await second.getByRole('button',{name:'基于当前版本继续编辑'}).click();
    let posts=0;
    await other.route('**/api/tools/authorization',async route => {posts++; await route.fetch(); await route.abort('failed');});
    await second.getByRole('button',{name:'保存授权',exact:true}).click();
    await expect(other.getByText(/提交结果尚未确认/)).toBeVisible();
    await expect(second).toContainText('服务端当前保存值：denied'); expect(posts).toBe(1);
    await other.unroute('**/api/tools/authorization');
    await page.getByRole('button',{name:'核对当前授权'}).click();
    await expect(card).toContainText('服务端当前保存值：denied');
    await card.getByLabel('external_fixture 授权草稿').selectOption('allowed');
    await server.send('apply-fail');
    await card.getByRole('button',{name:'保存授权',exact:true}).click();
    await expect(page.getByText(/已保存，尚未生效/)).toBeVisible();
    await expect(card).toContainText('authorization_not_applied');
    await server.send('apply-heal');
    await page.getByRole('button',{name:'重新应用已保存授权'}).click();
    await expect(card).toContainText('模型暴露：real');
  } finally {await other.close(); await server.close();}
});

test('A24 A32 A33: snapshot expiry, offline body, reconnect prune and page cleanup', async ({page,context}) => {
  const server=await fixture();
  try {
    await page.goto(server.origin+'/inbox');
    await page.getByRole('button',{name:'新建会话',exact:true}).click();
    await page.getByRole('button',{name:'展开对话',exact:true}).click();
    await page.getByLabel('消息',{exact:true}).fill('创建工具测试日程');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await expect(page.getByRole('region',{name:'主对话'})).toContainText('已查询到工具测试日程');
    await page.getByRole('link',{name:'Ops',exact:true}).click();
    await page.getByRole('button',{name:/查看账目 /}).first().click();
    await page.getByRole('button',{name:'展开模型结果投影'}).first().click();
    const preview=page.getByLabel('历史正文当前段');
    await expect(preview).toContainText('created');
    await page.getByRole('button',{name:'核验原操作当前结果',exact:true}).first().click();
    await expect(page.getByText(/当前核验（独立当前读数）/)).toHaveCount(1);
    await context.setOffline(true);
    await expect(page.getByText(/离线副本/)).toBeVisible();
    await expect(preview).toContainText('created');
    await server.send('prune');
    await context.setOffline(false);
    await expect(page.getByText(/历史不可读：trace_pruned/)).toBeVisible();
    await expect(preview).toBeEmpty();
    await page.getByRole('button',{name:'核验原操作当前结果',exact:true}).first().click();
    await expect(page.getByText(/当前核验（独立当前读数）/)).toHaveCount(2);
    await server.send('expire');
    await page.getByRole('button',{name:/查看账目 /}).first().click();
    await expect(page.getByText(/旧快照已失效/)).toBeVisible();
    await expect(page.getByLabel('账目汇总')).toContainText('Run 1');
    await page.getByRole('button',{name:'刷新账目'}).click();
    await expect(page.getByText('固定账目快照；后台变化后请显式刷新。')).toBeVisible();
  } finally {await context.setOffline(false); await server.close();}
});

import {writeFile} from 'node:fs/promises';
import {join} from 'node:path';

for (const trigger of ['focus', 'reconnect']) test(`A07: ${trigger} detects malformed authorization without claiming applied`, async ({page, context}) => {
  const server = await fixture();
  try {
    await page.goto(server.origin+'/tools');
    const card = externalCard(page);
    await card.getByLabel('external_fixture 授权草稿').selectOption('allowed');
    await card.getByRole('button', {name:'保存授权',exact:true}).click();
    await expect(card).toContainText('模型暴露：real');
    if (trigger === 'reconnect') {
      await context.setOffline(true);
      await expect(page.getByText(/离线，当前生效状态待核验/)).toBeVisible();
    }
    await writeFile(join(server.directory, 'tool_authorizations.json'), 'broken after load');
    if (trigger === 'focus') await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    else await context.setOffline(false);
    await expect(page.getByText(/配置：authorization_unreadable/)).toBeVisible();
    await expect(page.getByText(/生效：pending/)).toBeVisible();
    await expect(card).toContainText('模型暴露：hidden');
    await expect(card.getByRole('button', {name:'保存授权',exact:true})).toBeDisabled();
  } finally {await context.setOffline(false); await server.close();}
});

test('A23 A24: expired Ops to Run snapshot has explicit refresh preserving normalized filters', async ({page}) => {
  const server = await fixture();
  try {
    await page.goto(server.origin+'/inbox');
    await page.getByRole('button', {name:'新建会话',exact:true}).click();
    await page.getByRole('button', {name:'展开对话',exact:true}).click();
    await page.getByLabel('消息', {exact:true}).fill('创建工具测试日程');
    await page.getByRole('button', {name:'发送',exact:true}).click();
    await expect(page.getByRole('region', {name:'主对话'})).toContainText('已查询到工具测试日程');
    await page.getByRole('link', {name:'Tools',exact:true}).click();
    await page.locator('article').filter({has:page.getByRole('heading',{name:'create_event',exact:true})})
      .getByRole('link',{name:'查看包含该工具的运行'}).click();
    await page.getByRole('button',{name:/查看账目 /}).first().click();
    let href = await page.getByRole('link', {name:'进入运行过程（保留账目快照）'}).getAttribute('href');
    const oldSnapshot = new URL(href, server.origin).searchParams.get('snapshot_id');
    const before = await (await page.request.get(server.origin+'/api/ops?snapshot_id='+oldSnapshot)).json();
    const runId = before.runs[0].run_id;
    // Use every filter, then edit a draft without submitting it: the Run link
    // must carry the normalized snapshot filters, never these unsaved controls.
    await page.getByLabel('时间范围',{exact:true}).selectOption('custom');
    await page.getByLabel('IANA 时区',{exact:true}).fill('America/New_York');
    await page.getByLabel('开始日期（含）',{exact:true}).fill('2020-01-01');
    await page.getByLabel('结束日期（不含）',{exact:true}).fill('2030-01-01');
    await page.getByLabel('会话 ID',{exact:true}).fill(before.runs[0].session_id);
    await page.getByLabel('运行用途',{exact:true}).selectOption('chat');
    await page.getByLabel('Run ID',{exact:true}).fill(runId);
    const refreshed = page.waitForResponse(r => r.url().endsWith('/api/ops/snapshots'));
    await page.getByRole('button',{name:'刷新账目',exact:true}).click();
    const fixed = await (await refreshed).json();
    await page.getByRole('button',{name:/查看账目 /}).first().click();
    const link = page.getByRole('link',{name:'进入运行过程（保留账目快照）'});
    await expect(link).toBeVisible();
    await page.getByLabel('工具身份',{exact:true}).fill('unsaved-draft');
    await server.send('expire');
    const failedRead = page.waitForResponse(r => r.url().includes('/api/run-evidence?'));
    await link.click();
    expect((await failedRead).status()).toBe(410);
    await expect(page.getByText('账目快照已失效；尚未核验当前过程记录。',{exact:true})).toBeVisible();
    await expect(page.getByText('过程记录不可用',{exact:true})).toHaveCount(0);
    const ordinary = await page.request.get(server.origin+'/api/run-evidence?run_id='+runId);
    expect(ordinary.status()).toBe(200);
    expect((await ordinary.json()).trace_status).toBe('available');
    let snapshots = 0;
    page.on('request', r => {if (r.url().endsWith('/api/ops/snapshots')) snapshots++;});
    await page.getByRole('link',{name:'返回账本核对筛选并刷新',exact:true}).click();
    await expect(page.getByText('账目快照已失效；已保留原筛选，请显式刷新。',{exact:true})).toBeVisible();
    await expect(page.getByLabel('工具身份',{exact:true})).toHaveValue(fixed.filters.tool);
    await expect(page.getByLabel('会话 ID',{exact:true})).toHaveValue(fixed.filters.session_id);
    await expect(page.getByLabel('运行用途',{exact:true})).toHaveValue('chat');
    await expect(page.getByLabel('Run ID',{exact:true})).toHaveValue(runId);
    await expect(page.getByLabel('开始日期（含）',{exact:true})).toHaveValue('2020-01-01');
    expect(snapshots).toBe(0);
    const newRead = page.waitForResponse(r => r.url().endsWith('/api/ops/snapshots'));
    await page.getByRole('button',{name:'刷新账目',exact:true}).click();
    const renewed = await (await newRead).json();
    expect(renewed.filters).toEqual(fixed.filters);
    expect(renewed.snapshot_id).not.toBe(fixed.snapshot_id);
    await page.getByRole('button',{name:/查看账目 /}).first().click();
    const evidenceRead = page.waitForResponse(r => r.url().includes('/api/run-evidence?'));
    await page.getByRole('link',{name:'进入运行过程（保留账目快照）'}).click();
    const response = await evidenceRead;
    expect(response.status()).toBe(200);
    expect(new URL(response.url()).searchParams.get('snapshot_id')).toBe(renewed.snapshot_id);
    expect((await response.json()).trace_status).toBe('available');
  } finally {await server.close();}
});
