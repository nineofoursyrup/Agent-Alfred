import {test, expect} from "@playwright/test";

const deferred = () => {
  let resolve;
  const promise = new Promise(r => {resolve = r;});
  return {promise, resolve};
};
const result = page => page.getByRole("region", {name: "查询结果"});
const run = page => page.getByRole("button", {name: "执行", exact: true});
const cancel = page => page.getByRole("button", {name: "取消", exact: true});
async function open(page) {
  await page.goto('/database');
  await expect(page.getByText('可执行', {exact: true})).toBeVisible();
}
async function execute(page, sql) {
  await page.getByRole('textbox', {name: 'SQL', exact: true}).fill(sql);
  await run(page).click();
}

for (const previous of [false, true]) {
  test(`SPEC-11: cancel owns pending issuance, previous query=${previous}`, async ({page}) => {
    await open(page);
    if (previous) {
      await execute(page, 'SELECT 1 AS previous');
      await expect(result(page).locator('th')).toHaveText('previous');
    }
    const ready = deferred(), release = deferred();
    let id, executed = 0, cancelled;
    page.on('request', request => {if (request.url().endsWith('/execute')) executed++;});
    await page.route('**/api/database/queries', async route => {
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      id = (await response.json()).query_id;
      ready.resolve();
      await release.promise;
      await route.fulfill({response});
    });
    try {
      await execute(page, 'SELECT 42 AS cancelled_value');
      await ready.promise;
      await cancel(page).click();
      // Cancellation belongs to this click even before a query ID arrives.
      await expect(page.getByText('已请求取消', {exact: true})).toBeVisible();
      const receipt = page.waitForResponse(r => r.url().endsWith(`/${id}/cancel`));
      release.resolve();
      const response = await receipt;
      cancelled = await response.json();
      expect(cancelled.query_id).toBe(id);
      expect(cancelled.status).toBe('cancelled');
      expect(cancelled.cleanup).toBe('released');
      await expect(page.getByText('已实际停止', {exact: true})).toBeVisible();
      expect(executed).toBe(0);
      await expect(result(page)).toBeEmpty();
    } finally {release.resolve();}
  });
}

for (const delivery of ['before-B', 'after-B', 'new-page']) {
  test(`STD-05: cancel A receipt ${delivery} cannot change B`, async ({page}) => {
    await open(page);
    await execute(page, 'SELECT 1 AS A');
    await expect(result(page).locator('th')).toHaveText('A');
    const ready = deferred(), release = deferred();
    const bReady = deferred(), bRelease = deferred();
    let a;
    await page.route('**/api/database/queries/*/cancel', async route => {
      const response = await route.fetch();
      a = await response.json();
      expect(response.status()).toBe(200);
      expect(a.status).toBe('completed');
      ready.resolve();
      await release.promise;
      await route.fulfill({response});
    });
    try {
      await cancel(page).click();
      await ready.promise;
      if (delivery === 'new-page') {
        await page.getByRole('link', {name: '收件箱', exact: true}).click();
        await page.getByRole('link', {name: 'Database', exact: true}).click();
        await expect(page.getByText('可执行', {exact: true})).toBeVisible();
        await expect(page.getByRole('textbox', {name: 'SQL', exact: true})).toHaveValue('');
      }
      await page.route('**/api/database/queries/*/execute', async route => {
        const response = await route.fetch();
        const b = await response.json();
        expect(response.status()).toBe(200);
        expect(b.query_id).not.toBe(a.query_id);
        expect(b.columns).toEqual(['B']);
        bReady.resolve();
        if (delivery === 'before-B') await bRelease.promise;
        await route.fulfill({response});
      });
      await execute(page, 'SELECT 2 AS B');
      await bReady.promise;
      if (delivery !== 'before-B') await expect(result(page).locator('th')).toHaveText('B');
      const receipt = page.waitForResponse(r => r.url().endsWith(`/${a.query_id}/cancel`));
      release.resolve();
      await (await receipt).finished();
      // A rendering turn after the actual response delivery, not a timer.
      await page.evaluate(() => new Promise(requestAnimationFrame));
      if (delivery === 'before-B') {
        await expect(page.getByText('准备／执行中', {exact: true})).toBeVisible();
        bRelease.resolve();
      }
      await expect(result(page).locator('th')).toHaveText('B');
      await expect(result(page).locator('td')).toHaveText('2');
      await expect(page.getByText('结果完整', {exact: true})).toBeVisible();
    } finally {release.resolve(); bRelease.resolve();}
  });
}

test('SPEC-11: completion before cancel is honest and does not retain the result', async ({page}) => {
  await open(page);
  await execute(page, 'SELECT 1 AS A');
  await expect(result(page).locator('th')).toHaveText('A');
  await cancel(page).click();
  await expect(page.getByText('结果完整 · 取消前已完成', {exact: true})).toBeVisible();
  await expect(result(page)).toBeEmpty();
});

test('STD-05: late status probe for A cannot replace B', async ({page}) => {
  await open(page);
  const ready = deferred(), release = deferred();
  let a;
  await page.route('**/api/database/queries/*/execute', async route => {
    const response = await route.fetch();
    a = (await response.json()).query_id;
    await route.abort('failed');
  }, {times: 1});
  await page.route('**/api/database/queries/*', async route => {
    if (route.request().method() !== 'GET') return route.fallback();
    const response = await route.fetch();
    expect((await response.json()).query_id).toBe(a);
    ready.resolve();
    await release.promise;
    await route.fulfill({response});
  });
  try {
    await execute(page, 'SELECT 1 AS A');
    await ready.promise;
    await execute(page, 'SELECT 2 AS B');
    await expect(result(page).locator('th')).toHaveText('B');
    const receipt = page.waitForResponse(r => r.url().endsWith(`/${a}`));
    release.resolve();
    await (await receipt).finished();
    await page.evaluate(() => new Promise(requestAnimationFrame));
    await expect(result(page).locator('th')).toHaveText('B');
    await expect(page.getByText('结果完整', {exact: true})).toBeVisible();
  } finally {release.resolve();}
});
