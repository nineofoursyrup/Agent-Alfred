// Standalone native Chromium acceptance. No injected visibility/pageshow events,
// cache policy changes, focus emulation, disabled BFCache, sleeps or retries.
import {chromium, expect} from '@playwright/test';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {createServer} from 'node:http';
import {mkdtemp, writeFile, rm, readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {memoryServer} from './memory-server.js';

const output = resolve(process.argv[2] || 'output/playwright/native-lifecycle.json');
const evidence = {events: [], requests: [], notRestored: [], headers: []};
const profile = await mkdtemp(join(tmpdir(), 'issue66-native-'));
const args = ['--remote-debugging-port=0', '--no-first-run', '--no-default-browser-check',
  '--disable-component-update', '--disable-background-networking', `--user-data-dir=${profile}`,
  ...(process.env.NATIVE_HEADLESS === '1' ? ['--headless=new'] : []), 'about:blank'];
const child = spawn(chromium.executablePath(), args);
const closed = once(child, 'close');
let stderr = '', browser, server, control;
try {
  const endpoint = await new Promise((resolve, reject) => {
    child.stderr.on('data', data => {
      stderr += data;
      const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (match) resolve(match[1]);
    });
    closed.then(() => reject(new Error('Chromium exited before CDP: ' + stderr)));
  });
  browser = await chromium.connectOverCDP(endpoint, {noDefaults: true});
  evidence.browser = browser.version();
  evidence.launch = {arguments: args, noDefaults: true};
  const context = browser.contexts()[0];
  await context.addInitScript(() => {
    window.nativeDocument = crypto.randomUUID();
    window.nativeEvents = [];
    for (const name of ['visibilitychange', 'pageshow', 'pagehide', 'freeze', 'resume']) {
      const target = ['visibilitychange', 'freeze', 'resume'].includes(name) ? document : window;
      target.addEventListener(name, event => {
        window.nativeEvents.push({name, trusted: event.isTrusted, persisted: event.persisted ?? null,
          visibility: document.visibilityState, time: performance.now(), identity: window.nativeDocument});
      });
    }
  });
  server = await memoryServer({script: 'tests/browser/database_server.py'});
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Page.enable');
  cdp.on('Page.backForwardCacheNotUsed', event => evidence.notRestored.push(event));
  page.on('request', request => {
    if (request.url().endsWith('/api/database') || request.url().endsWith('/execute'))
      evidence.requests.push({url: request.url(), method: request.method(), time: performance.now()});
  });
  page.on('response', response => {
    if (response.url().endsWith('/database') || response.url().endsWith('/api/database'))
      evidence.headers.push({url: response.url(), status: response.status(), headers: response.headers()});
  });
  await page.goto(server.origin + '/database');
  await expect(page.getByText('可执行', {exact: true})).toBeVisible();
  const editor = page.getByRole('textbox', {name: 'SQL', exact: true});
  const results = page.getByRole('region', {name: '查询结果'});
  await editor.fill('SELECT 77 AS preserved');
  await page.getByRole('button', {name: '执行', exact: true}).click();
  await expect(results.locator('td')).toHaveText('77');
  await editor.fill('SELECT 99 AS draft_only');
  const other = await context.newPage();
  await other.goto('about:blank');
  await other.bringToFront();
  expect(await page.evaluate(() => document.visibilityState)).toBe('hidden');
  await expect(editor).toHaveValue('SELECT 99 AS draft_only');
  await expect(results.locator('td')).toHaveText('77');
  const hidden = await page.evaluate(() => ({visibility: document.visibilityState, events: window.nativeEvents}));
  await page.bringToFront();
  expect(await page.evaluate(() => document.visibilityState)).toBe('visible');
  await expect(editor).toHaveValue('SELECT 99 AS draft_only');
  await expect(results.locator('td')).toHaveText('77');
  evidence.hiddenResult = {hidden, restored: await page.evaluate(() => ({visibility: document.visibilityState, events: window.nativeEvents}))};
  expect(hidden.events.some(e => e.name === 'visibilitychange' && e.visibility === 'hidden' && e.trusted)).toBe(true);

  await server.send('arm');
  await editor.fill('SELECT * FROM diag_sessions');
  const timeoutResponse = page.waitForResponse(r => r.url().endsWith('/execute'));
  const start = performance.now();
  await page.getByRole('button', {name: '执行', exact: true}).click();
  await server.send('running');
  const running = JSON.parse(await readFile(join(server.directory, 'running.json'), 'utf8'));
  await other.bringToFront();
  expect(await page.evaluate(() => document.visibilityState)).toBe('hidden');
  const response = await timeoutResponse;
  const body = await response.json();
  const elapsed = performance.now() - start;
  expect(response.status()).toBe(504);
  expect(body.code).toBe('query_timeout');
  expect(body.query_id).toBe(running.query_id);
  expect(elapsed).toBeGreaterThanOrEqual(4800);
  // Original 5-second execution plus at most the specified 1-second cleanup.
  expect(elapsed).toBeLessThan(6000);
  expect(await page.evaluate(() => document.visibilityState)).toBe('hidden');
  const state = await (await page.request.get(`${server.origin}/api/database/queries/${running.query_id}`)).json();
  expect(state.status).toBe('timed_out');
  expect(state.cleanup).toBe('released');
  evidence.hiddenExecution = {running, elapsedMs: elapsed, execute: {http: response.status(), body}, status: state,
    events: await page.evaluate(() => window.nativeEvents)};
  await page.bringToFront();
  await expect(page.getByText('超时', {exact: true})).toBeVisible();
  await expect(editor).toHaveValue('SELECT * FROM diag_sessions');
  expect(evidence.requests.filter(r => r.url.endsWith('/execute')).length).toBe(2);
  await other.close();

  // Native cross-document navigation; the product's no-store headers and SSE
  // are unchanged. A blocked BFCache records the exact browser reason.
  await editor.fill('SELECT 88 AS before_history');
  await page.getByRole('button', {name: '执行', exact: true}).click();
  await expect(results.locator('td')).toHaveText('88');
  const before = await page.evaluate(() => ({identity: window.nativeDocument, events: window.nativeEvents}));
  const beforeQueries = evidence.requests.filter(r => r.url.endsWith('/execute')).length;
  const beforeCatalogs = evidence.requests.filter(r => r.url.endsWith('/api/database')).length;
  await page.goto(server.origin + '/inbox');
  await page.goBack({waitUntil: 'commit'});
  await expect(page.getByText('可执行', {exact: true})).toBeVisible();
  await expect(editor).toHaveValue('');
  await expect(results).toBeEmpty();
  const back = await page.evaluate(() => ({identity: window.nativeDocument, events: window.nativeEvents,
    navigation: performance.getEntriesByType('navigation').map(e => ({type: e.type, notRestoredReasons: e.notRestoredReasons?.toJSON?.()}))}));
  const persisted = back.events.some(e => e.name === 'pageshow' && e.persisted === true && e.trusted);
  evidence.bfcache = {status: persisted ? 'PASS' : 'BLOCKED', before, back,
    executesBefore: beforeQueries, executesAfter: evidence.requests.filter(r => r.url.endsWith('/execute')).length,
    catalogsBefore: beforeCatalogs, catalogsAfter: evidence.requests.filter(r => r.url.endsWith('/api/database')).length};
  expect(evidence.bfcache.executesAfter).toBe(beforeQueries);
  expect(evidence.bfcache.catalogsAfter).toBeGreaterThan(beforeCatalogs);
  if (persisted) expect(back.identity).toBe(before.identity);
  else {
    expect(back.identity).not.toBe(before.identity);
    expect(evidence.notRestored.length).toBeGreaterThan(0);
    evidence.bfcache.alternative = 'Actual native back navigation reload clears content and verifies catalog without execution; synthetic persisted-event regression remains supplemental. User judgment required; not BFCache PASS.';
  }

  // Positive control proves this unmodified browser can really use BFCache.
  control = createServer((req, res) => {
    res.writeHead(200, {'Content-Type': 'text/html', 'Cache-Control': 'public, max-age=60'});
    res.end(`<html><body><h1>Cacheable control ${req.url}</h1></body></html>`);
  });
  control.listen(0, '127.0.0.1');await once(control, 'listening');
  const controlOrigin = `http://127.0.0.1:${control.address().port}`;
  const controlPage = await context.newPage();
  await controlPage.goto(controlOrigin + '/a');
  const controlBefore = await controlPage.evaluate(() => window.nativeDocument);
  await controlPage.goto(controlOrigin + '/b');
  await controlPage.goBack({waitUntil: 'commit'});
  const controlBack = await controlPage.evaluate(() => ({identity: window.nativeDocument, events: window.nativeEvents}));
  evidence.bfcacheControl = {before: controlBefore, back: controlBack};
  expect(controlBack.identity).toBe(controlBefore);
  expect(controlBack.events.some(e => e.name === 'pageshow' && e.persisted === true && e.trusted)).toBe(true);
  evidence.hiddenAcceptance = 'PASS';
  console.log(JSON.stringify(evidence, null, 2));
} finally {
  await writeFile(output, JSON.stringify(evidence, null, 2));
  if (control) {control.closeAllConnections();await new Promise(r => control.close(r));}
  if (browser) await browser.close();
  child.kill('SIGTERM');await closed;
  if (server) await server.close();
  await rm(profile, {recursive: true, force: true});
}
