import {test, expect} from '@playwright/test';
import {memoryServer} from './memory-server.js';

test('CE-15 statistics is collapsed, independent of unsaved settings, and keyboard readable', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin + '/behaviour');
    const toggle = page.getByText('路由统计', {exact:true});
    await expect(toggle).toBeVisible();
    await expect(page.getByRole('button', {name:'刷新统计', exact:true})).toBeHidden();
    await page.getByRole('checkbox', {name:'启用消息分流'}).check();
    await page.getByLabel('聚合目标', {exact:true}).fill('保留未提交的目标');
    await toggle.focus(); await page.keyboard.press('Enter');
    await expect(page.getByText('此范围内没有已准入的聊天 Run。')).toBeVisible();
    await expect(page.getByRole('checkbox', {name:'启用消息分流'})).toBeChecked();
    await expect(page.getByLabel('统计范围')).toHaveValue('7d');
    await expect(page.getByLabel('聚合目标', {exact:true})).toHaveValue('保留未提交的目标');
    await page.setViewportSize({width:390, height:844});
    await page.getByLabel('统计范围').selectOption('all');
    await expect(page.getByText(/范围：全部/)).toBeVisible();
    await page.screenshot({path:'tmp/agent-work/issue-76/narrow.png', fullPage:true});
    await page.reload();
    await expect(page.getByRole('button', {name:'刷新统计', exact:true})).toBeHidden();
  } finally {await server.close();}
});

test('CE-03/08/14 real recovery counts survive restart and late range responses', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin + '/behaviour');
    await page.getByRole('checkbox', {name:'启用消息分流'}).check();
    await page.getByRole('button', {name:'保存设置', exact:true}).click();
    await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
    await server.send('corrupt-context');
    // Use the real MainBar because it also owns and supplies session identity.
    await page.getByRole('button', {name:'新建会话', exact:true}).click();
    await page.getByRole('button', {name:'展开对话', exact:true}).click();
    await page.getByRole('textbox', {name:'消息'}).fill('不用回复');
    await page.getByRole('button', {name:'发送', exact:true}).click();
    await expect(page.locator('#messages').getByText('已保存', {exact:true})).toBeVisible();
    await page.getByText('路由统计', {exact:true}).click();
    const panel = page.locator('.routing-statistics');
    await expect(panel.getByText(/上下文恢复：发生 1/)).toBeVisible();
    await expect(panel.locator('tr').filter({hasText:'不回复'})).toContainText('1/1');
    await server.restart(); await page.reload();
    await page.getByText('路由统计', {exact:true}).click();
    await expect(panel.getByText(/上下文恢复：发生 1/)).toBeVisible();
    let release, observed;
    const gate = new Promise(resolve => release = resolve);
    const started = new Promise(resolve => observed = resolve);
    await page.route('**/api/behaviour/routing-statistics?window=7d', async route => {
      const response = await route.fetch(); observed(); await gate;
      await route.fulfill({response}).catch(() => {});
    });
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await started;
    await page.getByLabel('统计范围').selectOption('24h');
    await expect(panel.getByText(/范围：24小时/)).toBeVisible();
    release();
    await page.unroute('**/api/behaviour/routing-statistics?window=7d');
    await expect(panel.getByText(/范围：24小时/)).toBeVisible();
    await page.route('**/api/behaviour/routing-statistics*', route => route.abort());
    await page.getByLabel('统计范围').selectOption('all');
    await expect(panel.getByText(/读取失败/)).toBeVisible();
    await expect(panel.getByText(/范围：24小时/)).toHaveCount(0);
    await page.unroute('**/api/behaviour/routing-statistics*');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel.getByText(/上下文恢复：发生 1/)).toBeVisible();
  } finally {await server.close();}
});

test('CE-14/15 closed and departed panels cannot be revived by late data; no polling', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  let release;
  try {
    await page.goto(server.origin + '/behaviour');
    let calls = 0;
    page.on('request', req => {if (req.url().includes('/api/behaviour/routing-statistics')) calls++;});
    await page.getByText('路由统计', {exact:true}).click();
    await expect(page.getByText('此范围内没有已准入的聊天 Run。')).toBeVisible();
    await page.clock.install(); await page.clock.fastForward(60_000);
    expect(calls).toBe(1);
    let ready, finished;
    const started = new Promise(resolve => ready = resolve);
    const gate = new Promise(resolve => release = resolve);
    const settled = new Promise(resolve => finished = resolve);
    await page.route('**/api/behaviour/routing-statistics*', async route => {
      const response = await route.fetch(); ready(); await gate;
      await route.fulfill({response}).catch(() => {}); finished();
    });
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await started;
    await page.getByText('路由统计', {exact:true}).click();
    release(); await settled;
    await expect(page.getByRole('button', {name:'刷新统计', exact:true})).toBeHidden();
    await expect(page.getByText('此范围内没有已准入的聊天 Run。')).toHaveCount(0);
    await page.unroute('**/api/behaviour/routing-statistics*');
    await page.goto(server.origin + '/runs');
    await expect(page.locator('.routing-statistics')).toHaveCount(0);
    await page.goto(server.origin + '/behaviour');
    await expect(page.getByRole('button', {name:'刷新统计', exact:true})).toBeHidden();
  } finally {release?.(); await server.close();}
});

test('CE-01 full failure and graph-internal fallback have separate live HTTP and page denominators', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin + '/behaviour');
    await page.getByRole('checkbox', {name:'启用消息分流'}).check();
    await page.getByRole('button', {name:'保存设置', exact:true}).click();
    await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
    await page.getByRole('button', {name:'展开对话', exact:true}).click();
    for (const task of ['STATS graph fallback', 'STATS full failure', 'STATS classifier failure']) {
      const previousSession = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
      await page.getByRole('button', {name:'新建会话', exact:true}).click();
      await expect.poll(() => page.evaluate(() => sessionStorage.getItem('alfred.session'))).not.toBe(previousSession);
      await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
      await page.getByRole('textbox', {name:'消息'}).fill(task);
      const admitted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
      await page.getByRole('button', {name:'发送', exact:true}).click();
      const response = await admitted, identity = await response.json();
      expect(response.status(), JSON.stringify(identity)).toBe(202);
      await expect.poll(async () => {
        const runs = await (await page.request.get(server.origin + '/api/runs?filter=chat&limit=25')).json();
        return runs.runs.find(r => r.run_id === identity.run_id)?.phase;
      }).toBe('finished');
      await expect(page.locator('#messages').getByText('已保存', {exact:true})).toBeVisible();
    }
    await page.getByText('路由统计', {exact:true}).click();
    const panel = page.locator('.routing-statistics');
    await expect(panel.locator('tr').filter({hasText:'图内保守兜底'})).toContainText('1/2');
    await expect(panel.locator('tr').filter({hasText:'完整回答'})).toContainText('1/2');
    await expect(panel.getByText(/图后故障回退：发生 2/)).toContainText('2/3');
    await expect(panel.getByText(/^终态 3/)).toContainText('明确未形成决议 1');
    await page.screenshot({path:'tmp/agent-work/issue-76/statistics.png', fullPage:true});
  } finally {await server.close();}
});

test('CE-15 statistics preserves a real aggregation request awaiting its receipt', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  let release;
  try {
    await page.goto(server.origin + '/behaviour');
    await page.getByRole('button', {name:'新建会话', exact:true}).click();
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem('alfred.session'))).not.toBeNull();
    const session = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
    await page.getByRole('button', {name:'刷新会话', exact:true}).click();
    await page.getByLabel('目标会话', {exact:true}).selectOption(session);
    await page.getByLabel('聚合目标', {exact:true}).fill('保持这份草稿请求');
    for (const name of ['语义记忆','情景记忆','会话窗口']) await page.getByRole('checkbox', {name, exact:true}).uncheck();
    let ready, completed;
    const started = new Promise(resolve => ready = resolve);
    const gate = new Promise(resolve => release = resolve);
    const settled = new Promise(resolve => completed = resolve);
    await page.route('**/api/runs', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      const response = await route.fetch(); expect(response.status()).toBe(202);
      ready(); await gate; await route.fulfill({response}); completed();
    });
    await page.getByRole('button', {name:'生成聚合草稿', exact:true}).click();
    await started;
    await page.getByText('路由统计', {exact:true}).click();
    await expect(page.getByText('此范围内没有已准入的聊天 Run。')).toBeVisible();
    await page.getByLabel('统计范围').selectOption('30d');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(page.getByText(/范围：30天/)).toBeVisible();
    await expect(page.getByRole('button', {name:'生成聚合草稿', exact:true})).toBeDisabled();
    await expect(page.getByLabel('聚合目标', {exact:true})).toHaveValue('保持这份草稿请求');
    release(); await settled;
    await expect(page.getByRole('button', {name:'生成聚合草稿', exact:true})).toBeEnabled();
  } finally {release?.(); await server.close();}
});

test('SPEC-02/CE-14 old process and departed-page responses cannot become current', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  let release;
  try {
    await page.goto(server.origin + '/behaviour');
    await page.getByRole('button', {name:'新建会话', exact:true}).click();
    const old = (await (await page.request.get(server.origin + '/api/entry')).json()).instance_id;
    let observed, finished;
    const started = new Promise(resolve => observed = resolve);
    const gate = new Promise(resolve => release = resolve);
    const settled = new Promise(resolve => finished = resolve);
    await page.route('**/api/behaviour/routing-statistics*', async route => {
      const response = await route.fetch(); observed(); await gate;
      await route.fulfill({response}).catch(() => {}); finished();
    });
    await page.getByText('路由统计', {exact:true}).click(); await started;
    const updated = page.waitForResponse(async r => r.url().endsWith('/api/entry') && r.ok() && (await r.json()).instance_id !== old);
    await server.restart(); const current = (await (await updated).json()).instance_id;
    release(); await settled;
    const panel = page.locator('.routing-statistics');
    await expect(panel).toContainText('进程已改变');
    await expect(panel).not.toContainText(old);
    await page.unroute('**/api/behaviour/routing-statistics*');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel).toContainText(current);
    let ready, done;
    const nextStarted = new Promise(resolve => ready = resolve);
    const nextGate = new Promise(resolve => release = resolve);
    const nextSettled = new Promise(resolve => done = resolve);
    await page.route('**/api/behaviour/routing-statistics*', async route => {
      const response = await route.fetch(); ready(); await nextGate;
      await route.fulfill({response}).catch(() => {}); done();
    });
    await page.getByRole('button', {name:'刷新统计', exact:true}).click(); await nextStarted;
    await page.getByRole('link', {name:'运行', exact:true}).click();
    release(); await nextSettled;
    await expect(panel).toHaveCount(0);
    await page.getByRole('link', {name:'Behaviour', exact:true}).click();
    await expect(page.getByRole('button', {name:'刷新统计', exact:true})).toBeHidden();
  } finally {release?.(); await server.close();}
});

async function enabled(page) {
  await page.getByRole('checkbox', {name:'启用消息分流'}).check();
  await page.getByRole('button', {name:'保存设置', exact:true}).click();
  await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
}
async function submitActual(page, server, message, saved = true) {
  const previous = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
  await page.getByRole('button', {name:'新建会话', exact:true}).click();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('alfred.session'))).not.toBe(previous);
  const expand = page.getByRole('button', {name:'展开对话', exact:true});
  if (await expand.isVisible()) await expand.click();
  await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
  await page.getByRole('textbox', {name:'消息'}).fill(message);
  await page.getByRole('button', {name:'发送', exact:true}).click();
  await expect(page.locator('#messages').getByText(saved ? '已保存' : '本次运行未保存', {exact:true})).toBeVisible();
}

test('SPEC-03 CE-05/12/13/15 legacy counts, unknown metrics and real bad-data errors', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin + '/behaviour'); await enabled(page);
    await submitActual(page, server, '你好');
    await server.send('legacy');
    await page.getByText('路由统计', {exact:true}).click();
    const panel = page.locator('.routing-statistics');
    const history = panel.locator('section').filter({hasText:'历史／版本未知'});
    await expect(history.locator('tr').filter({hasText:'快速回复'})).toContainText('1');
    await expect(history).not.toContainText('%');
    await expect(history).toContainText('仅列次数与缺失');
    await server.send('future-version');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(history).toContainText('未知决议 1');
    await expect(history).not.toContainText('%');
    await server.send('unknown-results');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel).toContainText('未知决议 1');
    await expect(panel).toContainText('缺少已持久保存的结果摘要');
    await expect(panel).toContainText('发生率 0/0 · 无可计算样本');
    await server.send('bad-time');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel).toContainText('样本归属无法判定');
    await expect(panel).not.toContainText('未知决议 1');
    await server.send('restore-data');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel.locator('tr').filter({hasText:'快速回复'})).toContainText('1/1');
    await server.send('offline-data');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel).toContainText('统计读取不可用');
    await server.send('online-data');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel.locator('tr').filter({hasText:'快速回复'})).toContainText('1/1');
  } finally {await server.close();}
});

test('SPEC-03 CE-06/15 disabled-only, settings conflict and real SQL timeout recover', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin + '/behaviour'); await submitActual(page, server, 'disabled run');
    await page.getByText('路由统计', {exact:true}).click();
    const panel = page.locator('.routing-statistics');
    await expect(panel).toContainText('此范围内没有可证明已启用的 Run');
    await expect(panel).toContainText('关闭 1');
    const {csrf_token} = await (await page.request.get(server.origin + '/api/entry')).json();
    const state = await (await page.request.get(server.origin + '/api/behaviour')).json();
    const changed = await page.request.post(server.origin + '/api/behaviour', {
      headers:{'x-agent-alfred-csrf':csrf_token}, data:{action:'save',enabled:true,expected_revision:state.revision},
    });
    expect(changed.status()).toBe(200);
    await page.getByRole('checkbox', {name:'启用消息分流'}).check();
    await page.getByRole('button', {name:'保存设置', exact:true}).click();
    await expect(page.getByText(/未保存.*conflict/)).toBeVisible();
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel).toContainText('关闭 1');
    await expect(page.getByText(/未保存.*conflict/)).toBeVisible();
    await expect(page.getByRole('checkbox', {name:'启用消息分流'})).toBeChecked();
    await server.send('lock');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel).toContainText('查询超过2秒工作预算');
    await expect(panel).not.toContainText('关闭 1');
    await server.send('unlock');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel).toContainText('关闭 1');
    await submitActual(page, server, '你好');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel).toContainText('已准入 2');
  } finally {await server.close();}
});

test('SPEC-03 CE-07/08 recorded failure recovery and context_failure reach the page', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin + '/behaviour'); await enabled(page);
    await server.send('corrupt-context'); await submitActual(page, server, 'task');
    await page.getByText('路由统计', {exact:true}).click();
    const panel = page.locator('.routing-statistics');
    await expect(panel.locator('tr').filter({hasText:'上下文失败提示'})).toContainText('1/1');
    await server.send('fail-recording'); await submitActual(page, server, '不用回复', false);
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel).toContainText('已持久终态 1 · 尚未记录终态 1');
    await server.send('repair-recording'); await server.restart(); await page.reload();
    await page.getByText('路由统计', {exact:true}).click();
    await expect(panel).toContainText('已持久终态 2 · 尚未记录终态 0');
    await expect(panel).toContainText('未知决议 1');
    await expect(panel.locator('tr').filter({hasText:'不回复'})).toContainText('0/1');
  } finally {await server.close();}
});

test('SPEC-03 CE-11 real Clock admission boundaries and rolling page window', async ({page}) => {
  const {writeFile} = await import('node:fs/promises');
  const server = await memoryServer({script:'tests/browser/routing_server.py', prepare:dir => writeFile(dir + '/fixed-clock','')});
  try {
    await page.goto(server.origin + '/behaviour'); await enabled(page);
    for (const stamp of ['2026-09-13T00:00:00Z','2026-09-12T23:59:59Z','2026-09-20T00:00:00Z','2026-09-20T07:59:59+08:00','2026-09-19T23:59:59+00:00']) {
      await server.send('clock ' + stamp); await submitActual(page, server, '你好');
    }
    await server.send('clock 2026-09-20T00:00:00Z');
    await page.getByText('路由统计', {exact:true}).click();
    const panel = page.locator('.routing-statistics');
    await expect(panel).toContainText('已准入 3');
    await server.send('clock 2026-09-27T00:00:00Z');
    await page.getByRole('button', {name:'刷新统计', exact:true}).click();
    await expect(panel).toContainText('已准入 1');
    await page.getByLabel('统计范围').selectOption('all');
    await expect(panel).toContainText('已准入 5');
  } finally {await server.close();}
});

test('SPEC-03 CE-16 complete real a-p sample is identical over HTTP and page', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/statistics_fixed_server.py'});
  try {
    await page.goto(server.origin + '/behaviour');
    const response = await page.request.get(server.origin + '/api/behaviour/routing-statistics?window=all');
    expect(response.status()).toBe(200);
    const data = await response.json(), group = data.groups[0];
    expect(data.sample).toEqual({admitted:13,enabled:11,disabled:1,unknown:1,finished:10,pending:1,unknown_bypass:1});
    expect(group.decisions.counts).toEqual({quick:1,full:3,fallback:1,no_action:1,context_failure:0});
    expect([group.decisions.known,group.decisions.none,group.decisions.unknown]).toEqual([6,2,2]);
    expect(group.fallback.rate).toEqual({numerator:2,denominator:8,value:0.25});
    expect(group.recovered.rate).toEqual({numerator:1,denominator:8,value:0.125});
    expect(group.blocked).toEqual({total:1,reasons:{side_effect_occurred:1}});
    await page.getByText('路由统计', {exact:true}).click();
    await page.getByLabel('统计范围').selectOption('all');
    const panel = page.locator('.routing-statistics');
    await expect(panel).toContainText('已准入 13 · 启用 11 · 关闭 1 · 启用未知 1');
    await expect(panel).toContainText('已持久终态 10 · 尚未记录终态 1');
    await expect(panel).toContainText('明确未形成决议 2 · 未知决议 2');
    await expect(panel.locator('tr').filter({hasText:'完整回答'})).toContainText('3/6 · 50%');
    await expect(panel).toContainText('发生率 2/8 · 25%');
    await expect(panel).toContainText('发生率 1/8 · 12.5%');
    await expect(panel).toContainText('覆盖 8/10 · 80%');
    await expect(panel).toContainText('图前绕行：1 · 未知 2');
    await expect(panel).toContainText('side_effect_occurred：1');
  } finally {await server.close();}
});

test('INTEGRATION-75 CE-14/15 statistics and both topologies retain independent state and close together', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  let release;
  try {
    await page.goto(server.origin + '/behaviour');
    await expect(page.getByRole('button', {name:'新建会话', exact:true})).toBeEnabled();
    const routing = page.getByRole('region', {name:'消息分流', exact:true});
    const aggregation = page.getByRole('region', {name:'手动聚合', exact:true});
    await routing.getByRole('button', {name:'查看流程', exact:true}).click();
    await aggregation.getByRole('button', {name:'查看流程', exact:true}).click();
    await expect(routing.locator('svg [data-node-id]')).toHaveCount(9);
    await expect(aggregation.locator('svg [data-node-id]')).toHaveCount(15);
    await routing.getByRole('button', {name:'判断消息类型 · classify', exact:true}).click();
    await routing.getByRole('button', {name:'放大', exact:true}).click();
    const viewport = await routing.locator('svg').getAttribute('viewBox');
    await page.getByRole('checkbox', {name:'启用消息分流'}).check();
    await page.getByRole('textbox', {name:'聚合目标', exact:true}).fill('两个观察面板均不提交此草稿');
    await routing.getByText('路由统计', {exact:true}).click();
    await expect(routing.getByText('此范围内没有已准入的聊天 Run。')).toBeVisible();
    await routing.getByLabel('统计范围').selectOption('all');
    await expect(routing.getByText(/范围：全部/)).toBeVisible();
    expect(await routing.locator('svg').getAttribute('viewBox')).toBe(viewport);
    await expect(routing.getByRole('heading', {name:'判断消息类型 · classify', exact:true})).toBeVisible();
    await expect(aggregation.locator('svg [data-node-id]')).toHaveCount(15);
    await expect(page.getByRole('checkbox', {name:'启用消息分流'})).toBeChecked();
    await expect(page.getByRole('textbox', {name:'聚合目标', exact:true})).toHaveValue('两个观察面板均不提交此草稿');
    expect((await (await page.request.get(server.origin + '/api/behaviour')).json()).enabled).toBe(false);
    expect((await (await page.request.get(server.origin + '/api/runs')).json()).runs).toEqual([]);
    let captured = 0, ready;
    const started = new Promise(resolve => ready = resolve);
    const gate = new Promise(resolve => release = resolve);
    const hold = async route => {
      const response = await route.fetch();
      if (++captured === 2) ready();
      await gate; await route.fulfill({response}).catch(() => {});
    };
    await page.route('**/api/behaviour/topology?workflow=message_routing', hold);
    await page.route('**/api/behaviour/routing-statistics*', hold);
    await routing.getByRole('button', {name:'重新读取', exact:true}).click();
    await routing.getByRole('button', {name:'刷新统计', exact:true}).click();
    await started;
    await page.getByRole('link', {name:'运行', exact:true}).click();
    release(); await page.unrouteAll({behavior:'wait'});
    await expect(page.locator('.topology, .routing-statistics')).toHaveCount(0);
    await page.getByRole('link', {name:'Behaviour', exact:true}).click();
    await expect(routing.getByRole('button', {name:'查看流程', exact:true})).toHaveAttribute('aria-expanded', 'false');
    await expect(aggregation.getByRole('button', {name:'查看流程', exact:true})).toHaveAttribute('aria-expanded', 'false');
    await expect(routing.getByRole('button', {name:'刷新统计', exact:true})).toBeHidden();
  } finally {release?.(); await server.close();}
});
