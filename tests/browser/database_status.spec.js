import {test, expect} from '@playwright/test';
import {memoryServer} from './memory-server.js';

for (const [kind, sql, http, terminal, expected] of [
  ['failed', 'SELECT abs(-9223372036854775808)', 400, 'failed', '执行失败'],
  ['timed_out', 'SELECT * FROM diag_sessions', 504, 'timed_out', '超时'],
  ['completed', 'SELECT 12 AS unseen', 200, 'completed', '结果未收到'],
]) {
  test(`STD-06/SPEC-12: real lost execute response ${kind}/released`, async ({page}) => {
    const server = await memoryServer({script: 'tests/browser/database_server.py'});
    const evidence = {kind};
    try {
      await page.goto(server.origin + '/database');
      await expect(page.getByText('可执行', {exact: true})).toBeVisible();
      if (kind === 'timed_out') await server.send('arm');
      await page.route('**/api/database/queries/*/execute', async route => {
        const response = await route.fetch();
        evidence.execute = {http: response.status(), body: await response.json()};
        await route.abort('failed');
      });
      let read;
      const status = new Promise(resolve => {read = resolve;});
      await page.route('**/api/database/queries/*', async route => {
        if (route.request().method() !== 'GET') return route.fallback();
        const response = await route.fetch();
        evidence.status = {http: response.status(), body: await response.json()};
        await route.fulfill({response});
        read();
      });
      await page.getByRole('textbox', {name: 'SQL', exact: true}).fill(sql);
      await page.getByRole('button', {name: '执行', exact: true}).click();
      await status;
      expect(evidence.execute.http).toBe(http);
      expect(evidence.status.http).toBe(200);
      expect(evidence.status.body.query_id).toBe(evidence.execute.body.query_id);
      expect(evidence.status.body.status).toBe(terminal);
      expect(evidence.status.body.cleanup).toBe('released');
      console.log('real-status-loss', JSON.stringify(evidence));
      await expect(page.getByText(expected, {exact: true})).toBeVisible();
      await expect(page.getByRole('region', {name: '查询结果'})).toBeEmpty();
    } finally {await server.close();}
  });
}

// Exhaustive presentation matrix is supplemental to real HTTP/worker cases.
// This does not claim that a fabricated status proves lifecycle cleanup.
for (const cleanup of ['released', 'pending', 'failed']) {
  test(`status/cleanup presentation matrix: ${cleanup}`, async ({page}) => {
    await page.goto('/database');
    await expect(page.getByText('可执行', {exact: true})).toBeVisible();
    let state = 'unused';
    await page.route('**/api/database/queries/*/execute', route => route.abort('failed'));
    await page.route('**/api/database/queries/*', async route => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({json: {status: state, cleanup}});
    });
    for (const [value, expected] of [
      ['unused', '尚未执行'], ['prepared', '准备／执行中'], ['running', '准备／执行中'],
      ['stopping', '已请求取消'], ['completed', '结果未收到'],
      ['cancelled', cleanup === 'released' ? '已实际停止' : '已请求取消'],
      ['timed_out', '超时'], ['invalidated', '失效'], ['failed', '执行失败'],
    ]) {
      state = value;
      await page.goto('/database');
      await expect(page.getByText('可执行', {exact:true})).toBeVisible();
      const response = page.waitForResponse(r => r.request().method() === 'GET' && /\/api\/database\/queries\/[^/]+$/.test(new URL(r.url()).pathname));
      await page.getByRole('button', {name: '执行', exact: true}).click();
      await response;
      await expect(page.locator('#page [data-state]')).toHaveText(expected);
      await expect(page.getByText(cleanup === 'failed' ? '清理失败' : cleanup === 'pending' ? '清理中' : '清理已释放', {exact:true})).toBeVisible();
      if (cleanup !== 'released') await expect(page.getByRole('button',{name:'执行',exact:true})).toBeDisabled();
      await expect(page.getByRole('region', {name: '查询结果'})).toBeEmpty();
    }
  });
}

for (const [code, http, expected] of [
  ['handle_expired', 410, '不可用 · 句柄已过期'],
  ['not_found', 404, '不可用 · 没有这条查询记录'],
  ['instance_changed', 409, '不可用 · 实例已更换'],
]) {
  test(`status lookup error ${code} is not a cleanup failure`, async ({page}) => {
    await page.goto('/database');
    await expect(page.getByText('可执行', {exact: true})).toBeVisible();
    await page.route('**/api/database/queries/*/execute', route => route.abort('failed'));
    await page.route('**/api/database/queries/*', async route => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({status: http, json: {code}});
    });
    await page.getByRole('button', {name: '执行', exact: true}).click();
    await expect(page.locator('#page [data-state]')).toHaveText(expected);
  });
}

test('STD-06/SPEC-12: actual worker stop failure is cleanup failure', async ({page}) => {
  const server = await memoryServer({script: 'tests/browser/database_server.py'});
  try {
    await page.goto(server.origin + '/database');
    await expect(page.getByText('可执行', {exact: true})).toBeVisible();
    await server.send('arm');
    await server.send('kill-fail');
    await page.getByRole('textbox', {name: 'SQL', exact: true}).fill('SELECT * FROM diag_sessions');
    await page.getByRole('button', {name: '执行', exact: true}).click();
    await server.send('running');
    const cancelled = page.waitForResponse(r => r.url().endsWith('/cancel'));
    await page.getByRole('button', {name: '取消', exact: true}).click();
    const response = await cancelled;
    const body = await response.json();
    expect(body.cleanup).toBe('failed');
    expect(body.status).toBe('failed');
    await expect(page.getByText('清理失败', {exact: true})).toBeVisible();
    const status = await (await page.request.get(`${server.origin}/api/database/queries/${body.query_id}`)).json();
    expect(status.cleanup).toBe('failed');
    const entry = await (await page.request.get(server.origin + '/api/entry')).json();
    const denied = await page.request.post(server.origin + '/api/database/queries', {
      headers: {'x-agent-alfred-csrf': entry.csrf_token}, data: {},
    });
    expect(denied.status()).toBe(503);
    expect((await denied.json()).code).toBe('cleanup_failed');
    console.log('actual-cleanup-failure', JSON.stringify({cancel: body, status, newHandleStatus: denied.status()}));
    await server.send('heal');
    const recovered = await (await page.request.get(`${server.origin}/api/database/queries/${body.query_id}`)).json();
    expect(recovered.cleanup).toBe('released');
  } finally {await server.close();}
});

test('S10 failed cleanup blocks queries until both release and capability are verified', async ({page}) => {
  const server = await memoryServer({script: 'tests/browser/database_server.py'});
  try {
    await page.goto(server.origin + '/database');
    await expect(page.getByText('可执行', {exact:true})).toBeVisible();
    await server.send('arm');
    await server.send('kill-fail');
    await page.getByRole('textbox', {name:'SQL',exact:true}).fill('SELECT * FROM diag_sessions');
    await page.getByRole('button', {name:'执行',exact:true}).click();
    await server.send('running');
    const cancelled = page.waitForResponse(r => r.url().endsWith('/cancel'));
    await page.getByRole('button', {name:'取消',exact:true}).click();
    const receipt = await (await cancelled).json();
    expect(receipt.cleanup).toBe('failed');
    await expect(page.getByText('清理失败',{exact:true})).toBeVisible();
    const run = page.getByRole('button', {name:'执行',exact:true});
    await expect(run).toBeDisabled();
    let newHandles = 0;
    page.on('request', r => {if(r.url().endsWith('/api/database/queries')) newHandles++;});
    await run.evaluate(el => el.click());
    expect(newHandles).toBe(0);
    await page.getByRole('button', {name:'核验查询与可用性',exact:true}).click();
    await expect(run).toBeDisabled();
    await server.send('heal');
    await page.getByRole('button', {name:'核验查询与可用性',exact:true}).click();
    await expect(page.getByText('清理已释放',{exact:true})).toBeVisible();
    await expect(run).toBeEnabled();
    await page.getByRole('textbox', {name:'SQL',exact:true}).fill('SELECT 7 AS recovered');
    await run.click();
    await expect(page.getByRole('region', {name:'查询结果'}).locator('td')).toHaveText('7');
  } finally {await server.close();}
});

for (const lost of ['issuance', 'execute']) {
  test(`S10 lost ${lost} receipt preserves the boundary for explicit capability recovery`, async ({page}, testInfo) => {
    const server = await memoryServer();
    const evidence = {lost, handles:[], executes:0, lostStatusReads:0};
    try {
      await page.goto(server.origin+'/database');
      await expect(page.getByText('可执行',{exact:true})).toBeVisible();
      const sql = page.getByRole('textbox',{name:'SQL',exact:true});
      const run = page.getByRole('button',{name:'执行',exact:true});
      const recheck = page.getByRole('button',{name:'核验查询与可用性',exact:true});
      page.on('request', request => {if(request.url().endsWith('/execute')) evidence.executes++;});
      await page.route('**/api/database/queries', async route => {
        const response = await route.fetch();
        expect(response.status()).toBe(200);
        evidence.handles.push((await response.json()).query_id);
        if (lost === 'issuance' && evidence.handles.length === 1) await route.abort('failed');
        else await route.fulfill({response});
      });
      if (lost === 'execute') {
        await page.route('**/api/database/queries/*/execute', async route => {
          const response = await route.fetch();
          expect(response.status()).toBe(200);
          await route.abort('failed');
        }, {times:1});
        await page.route('**/api/database/queries/*', async route => {
          if(route.request().method() !== 'GET') return route.fallback();
          evidence.lostStatusReads++;
          await route.abort('failed');
        });
      }
      await sql.fill('SELECT 42 AS first_draft');
      await run.click();
      await expect(page.locator('[data-state]')).toHaveAttribute('data-state','unavailable');
      await expect(run).toBeDisabled();
      await expect(sql).toHaveValue('SELECT 42 AS first_draft');
      expect(evidence.handles).toHaveLength(1);
      expect(evidence.executes).toBe(lost === 'issuance' ? 0 : 1);
      // Independent public read observes the real handle; the browser has not
      // received this identity/status and must not use test knowledge as proof.
      evidence.actual = await (await page.request.get(server.origin+'/api/database/queries/'+evidence.handles[0])).json();
      expect(evidence.actual.status).toBe(lost === 'issuance' ? 'unused' : 'completed');
      expect(evidence.actual.cleanup).toBe('released');
      const catalog = await (await page.request.get(server.origin+'/api/database')).json();
      evidence.capability = {instance:catalog.instance_id, available:catalog.available};
      expect(catalog.available).toBe(true);
      expect(catalog.instance_id).toBe(evidence.actual.instance_id);
      if(lost === 'issuance') {
        const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/database');
        await recheck.click();
        expect((await (await response).json()).available).toBe(true);
        await expect(run).toBeEnabled();
        await expect(page.getByText('可执行',{exact:true})).toBeVisible();
      } else {
        const failed = page.waitForEvent('requestfailed', {predicate:r => r.method()==='GET' && r.url().endsWith('/'+evidence.handles[0])});
        await recheck.click();
        await failed;
        await expect(run).toBeDisabled();
        expect(evidence.lostStatusReads).toBe(2);
        expect(evidence.handles).toHaveLength(1);
        expect(evidence.executes).toBe(1);
        // A real known-handle status, not merely catalog availability, is
        // required after the browser has sent execution.
        await page.unroute('**/api/database/queries/*');
        await recheck.click();
        await expect(run).toBeEnabled();
        await expect(page.getByText('结果未收到',{exact:true})).toBeVisible();
      }
      await expect(page.getByRole('region',{name:'查询结果',exact:true})).toBeEmpty();
      expect(evidence.handles).toHaveLength(1);
      expect(evidence.executes).toBe(lost === 'issuance' ? 0 : 1);
      await sql.fill('SELECT 84 AS explicit_next');
      await run.click();
      await expect(page.getByRole('region',{name:'查询结果',exact:true}).locator('td')).toHaveText('84');
      expect(evidence.handles).toHaveLength(2);
      expect(evidence.executes).toBe(lost === 'issuance' ? 1 : 2);
      console.log('lost-receipt-recovery',JSON.stringify(evidence));
    } finally {
      try {await page.close();} finally {await server.close();}
      await testInfo.attach('actual-receipt-recovery',{body:JSON.stringify(evidence,null,2),contentType:'application/json'});
    }
  });
}
