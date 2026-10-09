import {test, expect} from '@playwright/test';

test('Overview, Run list and details distinguish all known purposes from an unknown purpose', async ({page}) => {
  // These are the closed Run purpose values from _schema/contracts.py, plus a
  // future value. Only the HTTP read payload is controlled; renderers are real.
  const purposes = [
    ['chat', '普通聊天'],
    ['inference_probe', '推理探针'],
    ['consolidation', '记忆提炼'],
    ['aggregation', '手动聚合'],
    ['future-purpose', null],
  ];
  const runs = purposes.map(([purpose, label]) => ({
    run_id: `purpose-${purpose}`,
    purpose,
    purpose_known: label !== null,
    filter: ['chat', 'aggregation'].includes(purpose) ? 'chat' : 'system',
    gateway: 'cli',
    session_id: null,
    admission_state: 'admitted',
    phase: 'finished',
    outcome: 'completed',
    recording_state: 'recorded',
    accepted_at: '2026-10-09T00:00:00+00:00',
    started_at: '2026-10-09T00:00:00+00:00',
    finished_at: '2026-10-09T00:00:01+00:00',
  }));
  const listing = {filter: 'all', runs, non_terminal: null, next_cursor: null};
  await page.route('**/api/runs?*', route => route.fulfill({json: listing}));
  await page.route('**/api/runs/locate/*', route => route.fulfill({json: listing}));
  await page.route('**/api/run-evidence?*', route => route.fulfill({json: {
    run_id: new URL(route.request().url()).searchParams.get('run_id'),
    trace_status: 'unavailable', events: [], attempts: [],
  }}));
  await page.route('**/api/overview/recent-runs?*', async route => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({response, json: {...body, runs}});
  });

  await page.goto('/overview');
  await expect(page.locator('.overview-run')).toHaveCount(purposes.length);
  for (const [purpose, label] of purposes) {
    const card = page.locator(`.overview-run[data-run-id="purpose-${purpose}"]`);
    await expect(card).toContainText(label ?? `未知用途：${purpose}`);
    if (label !== null) await expect(card).not.toContainText('未知用途');
  }
  await page.getByRole('link', {name: '查看全部运行', exact: true}).click();
  for (const [purpose, label] of purposes) {
    const expected = label === null ? `${purpose}（未知用途）` : `${label}（${purpose}）`;
    const row = page.getByRole('table', {name: '运行记录'}).locator(`[data-source-id="purpose-${purpose}"]`);
    await expect(row.getByText(expected, {exact: true})).toBeVisible();
  }
  // Read each detail directly: restoring a list source has a separate identity
  // contract and is covered by the source-navigation tests.
  for (const [purpose, label] of purposes) {
    const expected = label === null ? `${purpose}（未知用途）` : `${label}（${purpose}）`;
    await page.goto(`/runs/${encodeURIComponent(`purpose-${purpose}`)}?filter=all`);
    const summary = page.getByRole('region', {name: '运行摘要', exact: true});
    await expect(summary.getByText(expected, {exact: true})).toBeVisible();
    if (label !== null) await expect(summary).not.toContainText('未知用途');
  }
});
