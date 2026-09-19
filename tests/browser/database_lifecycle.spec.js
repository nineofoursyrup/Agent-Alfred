import {test, expect} from '@playwright/test';
import {readFile} from 'node:fs/promises';
import {join} from 'node:path';
import {memoryServer} from './memory-server.js';

for (const action of ['reload', 'close']) {
  test(`AC-25: real ${action} cancels an actual worker and releases all owners`, async ({page}, testInfo) => {
    const server = await memoryServer({script: 'tests/browser/database_server.py'});
    try {
      await page.goto(server.origin + '/database');
      await expect(page.getByText('可执行', {exact: true})).toBeVisible();
      await server.send('arm');
      await page.getByRole('textbox', {name: 'SQL', exact: true}).fill('SELECT * FROM diag_sessions');
      await page.getByRole('button', {name: '执行', exact: true}).click();
      await server.send('running');
      const before = JSON.parse(await readFile(join(server.directory, 'running.json'), 'utf8'));
      expect(before.status).toBe('running');
      expect(before.cleanup).toBe('pending');
      if (action === 'reload') await page.reload();
      else await page.close({runBeforeUnload: true});
      await server.send('released');
      const after = JSON.parse(await readFile(join(server.directory, 'released.json'), 'utf8'));
      console.log("actual-lifecycle", action, JSON.stringify({before, after}));
      expect(after.query_id).toBe(before.query_id);
      expect(after.pid_exists).toBe(false);
      expect(after.cleanup).toBe('released');
      await testInfo.attach(`actual-${action}-resources`, {body: JSON.stringify({before, after}), contentType: 'application/json'});
      if (action === 'reload') {
        await expect(page.getByText('可执行', {exact: true})).toBeVisible();
        await expect(page.getByRole('textbox', {name: 'SQL', exact: true})).toHaveValue('');
        await expect(page.getByRole('region', {name: '查询结果'})).toBeEmpty();
        await page.getByRole('textbox', {name: 'SQL', exact: true}).fill('SELECT 7 AS fresh');
        await page.getByRole('button', {name: '执行', exact: true}).click();
        await expect(page.getByRole('region', {name: '查询结果'}).locator('td')).toHaveText('7');
      }
    } finally {await server.close();}
  });
}

test('AC-25: real browser offline and online clears results, preserves SQL, never reruns', async ({page, context}) => {
  await page.goto('/database');
  await expect(page.getByText('可执行', {exact: true})).toBeVisible();
  let executes = 0;
  page.on('request', r => {if (r.url().endsWith('/execute')) executes++;});
  const sql = page.getByRole('textbox', {name: 'SQL', exact: true});
  const results = page.getByRole('region', {name: '查询结果'});
  await sql.fill('SELECT 7 AS online_value');
  await page.getByRole('button', {name: '执行', exact: true}).click();
  await expect(results.locator('td')).toHaveText('7');
  await context.setOffline(true);
  await expect(page.getByText('不可用 · 连接中断', {exact: true})).toBeVisible();
  await expect(results).toBeEmpty();
  await expect(sql).toHaveValue('SELECT 7 AS online_value');
  await expect(page.getByRole('button', {name: '执行', exact: true})).toBeDisabled();
  await context.setOffline(false);
  await expect(page.getByText('可执行', {exact: true})).toBeVisible();
  await expect(results).toBeEmpty();
  await expect(sql).toHaveValue('SELECT 7 AS online_value');
  expect(executes).toBe(1);
});
