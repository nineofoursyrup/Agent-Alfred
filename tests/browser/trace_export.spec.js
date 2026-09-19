import {test, expect} from '@playwright/test';
import {execFileSync} from 'node:child_process';

test('real Run exports an inert share ZIP through native browser download', async ({page}) => {
  await page.goto('/');
  await page.getByRole('button', {name:'新建会话',exact:true}).click();
  await page.getByRole('button', {name:'展开对话',exact:true}).click();
  await page.getByRole('textbox', {name:'消息'}).fill('export private prompt');
  const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.status() === 202);
  await page.getByRole('button', {name:'发送',exact:true}).click();
  const {run_id} = await (await accepted).json();
  await expect(page.getByRole('region', {name:'主对话'})).toContainText('已保存');
  await page.goto(`/runs/${run_id}`);
  await page.getByRole('button', {name:'生成追踪导出',exact:true}).click();
  await expect(page.getByRole('button', {name:'下载 ZIP',exact:true})).toBeVisible();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', {name:'下载 ZIP',exact:true}).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^alfred-trace-[0-9a-f]+\.zip$/);
  const path = await download.path();
  execFileSync('uv', ['run','python','-c', `
import zipfile,json,hashlib,sys
with zipfile.ZipFile(sys.argv[1]) as z:
 m=json.loads(z.read('manifest.json'))
 assert m['source_integrity']=='verified_complete',m
 assert 'README.txt' in z.namelist()
 for name, info in m['files'].items():
  raw=z.read(name)
  assert len(raw)==info['bytes']
  assert hashlib.sha256(raw).hexdigest()==info['sha256']
 for info in z.infolist():
  assert info.date_time==(1980,1,1,0,0,0)
  assert not info.filename.startswith('/') and '..' not in info.filename.split('/')
  assert b'export private prompt' not in z.read(info)
  assert sys.argv[2].encode() not in z.read(info)
`, path, run_id]);
});

async function createRun(page) {
  await page.goto('/');
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  await page.getByRole('button',{name:'展开对话',exact:true}).click();
  await page.getByRole('textbox',{name:'消息'}).fill('export lifecycle');
  const accepted=page.waitForResponse(r=>r.url().endsWith('/api/runs')&&r.status()===202);
  await page.getByRole('button',{name:'发送',exact:true}).click();
  const {run_id}=await (await accepted).json();
  await expect(page.getByRole('region',{name:'主对话'})).toContainText('已保存');
  await page.goto(`/runs/${run_id}`);
  return run_id;
}

test('AC18 AC24: another tab sees busy, leaving ready releases ownership',async({page,context})=>{
  const id=await createRun(page);
  await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
  await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toBeVisible();
  const other=await context.newPage();
  await other.goto(`/runs/${id}`);
  await other.getByRole('button',{name:'生成追踪导出',exact:true}).click();
  await expect(other.getByRole('region',{name:'追踪导出'})).toContainText('已有导出任务');
  const cancelled=page.waitForResponse(r=>r.url().includes('/api/trace-exports/')&&r.url().endsWith('/cancel'));
  await page.getByRole('link',{name:'运行',exact:true}).click();
  expect((await (await cancelled).json()).cleanup).toBe('released');
  await other.getByRole('button',{name:'生成追踪导出',exact:true}).click();
  await expect(other.getByRole('button',{name:'下载 ZIP',exact:true})).toBeVisible();
  await other.getByRole('button',{name:'取消导出',exact:true}).click();
  await expect(other.getByRole('region',{name:'追踪导出'})).toContainText('已取消');
  await other.close();
});

test('AC24: delayed real creation response cannot restore a departed page',async({page})=>{
  await createRun(page);
  let release,arrived;
  const gate=new Promise(r=>{release=r;}),ready=new Promise(r=>{arrived=r;});
  let taskId;
  await page.route('**/api/trace-exports',async route=>{
    const response=await route.fetch();expect(response.status()).toBe(202);
    taskId=(await response.json()).task_id;arrived();
    await gate;await route.fulfill({response});
  });
  try {
    await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await ready;
    await page.getByRole('link',{name:'运行',exact:true}).click();
    const cancel=page.waitForResponse(r=>r.url().endsWith(`/${taskId}/cancel`));
    release();
    expect((await (await cancel).json()).cleanup).toBe('released');
    await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toHaveCount(0);
  } finally {release();}
});

import {memoryServer} from './memory-server.js';

test('AC02 AC09 AC10 AC12: multistep Attempts, inline and external tool bodies download in both modes',async({page})=>{
  const server=await memoryServer({script:'tests/browser/trace_export_server.py'});
  try {
    await page.goto(server.origin+'/');
    await page.getByRole('button',{name:'新建会话',exact:true}).click();
    await page.getByRole('button',{name:'展开对话',exact:true}).click();
    await page.getByRole('textbox',{name:'消息'}).fill('导出工具验收');
    const accepted=page.waitForResponse(r=>r.url().endsWith('/api/runs')&&r.status()===202);
    await page.getByRole('button',{name:'发送',exact:true}).click();
    const {run_id}=await (await accepted).json();
    await expect(page.getByRole('region',{name:'主对话'})).toContainText('已保存');
    await server.send('protect');
    await page.goto(server.origin+`/runs/${run_id}`);
    for (const mode of ['share','diagnostic']) {
      await page.getByRole('combobox',{name:'导出模式'}).selectOption(mode);
      if (mode==='diagnostic') await expect(page.getByRole('region',{name:'追踪导出'})).toContainText('仍可能含私人信息');
      await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
      await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toBeVisible();
      const received=page.waitForEvent('download');
      await page.getByRole('button',{name:'下载 ZIP',exact:true}).click();
      const download=await received;
      const path=await download.path();
      execFileSync('uv',['run','python','-c',`
import zipfile,json,hashlib,sys
with zipfile.ZipFile(sys.argv[1]) as z:
 m=json.loads(z.read('manifest.json'))
 assert m['source_integrity']=='verified_complete',m
 ev=[json.loads(line) for line in z.read('bundle/trace.jsonl').splitlines()]
 assert len([e for e in ev if e['payload_name']=='attempt.committed'])>=3
 tools=[e for e in ev if e['payload_name']=='tool.finished']
 assert len(tools)==2
 ref=next(e['payload']['audit_content'] for e in tools if isinstance(e['payload']['audit_content'],dict))
 artifact=z.read('bundle/'+ref['artifact'])
 assert len(artifact)==ref['bytes'] and hashlib.sha256(artifact).hexdigest()==ref['sha256']
 all_bytes=b''.join(z.read(n) for n in z.namelist())
 assert b'newly-secret' not in all_bytes
 assert sys.argv[3].encode() not in all_bytes
 assert sys.argv[4].encode() not in all_bytes
 if sys.argv[2]=='share':assert '诊断正文🙂'.encode() not in all_bytes
 else:
  assert artifact.count('诊断正文🙂<script>not executed</script>'.encode())==6000
  assert len(artifact)>256*1024
`,path,mode,run_id,server.directory]);
      await expect(page.getByRole('region',{name:'追踪导出'})).toContainText('服务端传输结束');
    }
  } finally {await server.close();}
});

async function fixtureRun(page,server,message='导出工具验收',saved=true) {
  await page.goto(server.origin+'/');
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  const expand=page.getByRole('button',{name:'展开对话',exact:true});
  if (await expand.isVisible()) await expand.click();
  await page.getByRole('textbox',{name:'消息'}).fill(message);
  const accepted=page.waitForResponse(r=>r.url().endsWith('/api/runs')&&r.status()===202);
  await page.getByRole('button',{name:'发送',exact:true}).click();
  const {run_id}=await (await accepted).json();
  if (saved) await expect(page.getByRole('region',{name:'主对话'})).toContainText('已保存');
  await page.goto(server.origin+`/runs/${run_id}`);
  return run_id;
}

async function nativeZip(page) {
  const download=page.waitForEvent('download');
  await page.getByRole('button',{name:'下载 ZIP',exact:true}).click();
  return await download;
}

function zipManifest(path, expected) {
  execFileSync('uv',['run','python','-c',`
import sys,zipfile,json,hashlib
with zipfile.ZipFile(sys.argv[1]) as z:
 m=json.loads(z.read('manifest.json'))
 assert m['source_integrity']==sys.argv[2],m
 assert sys.argv[2].encode() in z.read('README.txt')
 for name,info in m['files'].items():
  data=z.read(name)
  assert len(data)==info['bytes'] and hashlib.sha256(data).hexdigest()==info['sha256']
 for missing in m['missing']:
  assert missing.encode() in z.read('README.txt')
`,path,expected]);
}

test('SPEC03 CE01: missing artifact warns in page and real downloaded manifest and README',async({page})=>{
  const server=await memoryServer({script:'tests/browser/trace_export_server.py'});
  try {
    await fixtureRun(page,server);
    await server.send('missing-artifact');
    await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await expect(page.getByRole('region',{name:'追踪导出'})).toContainText('known_incomplete');
    await expect(page.getByRole('region',{name:'追踪导出'})).toContainText('artifact-1_missing');
    const download=await nativeZip(page);
    zipManifest(await download.path(),'known_incomplete');
  } finally {await server.close();}
});

test('SPEC03 CE08 CE06: HTTP cancel keeps active IO busy in second tab; generation replacement rejects',async({page,context})=>{
  const server=await memoryServer({script:'tests/browser/trace_export_server.py'});
  let other;
  try {
    const rid=await fixtureRun(page,server);
    await server.send('hold-read');
    await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await server.send('await-read');
    await page.getByRole('button',{name:'取消导出',exact:true}).click();
    await expect(page.getByRole('region',{name:'追踪导出'})).toContainText('正在清理');
    other=await context.newPage();await other.goto(server.origin+`/runs/${rid}`);
    await other.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await expect(other.getByRole('region',{name:'追踪导出'})).toContainText('已有导出任务');
    await server.send('release-read');
    await expect(page.getByRole('region',{name:'追踪导出'})).toContainText('已取消');
    await server.send('hold-read');
    await other.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await server.send('await-read');
    await server.send('replace-source');
    const terminal=other.waitForResponse(async r=>r.url().includes('/api/trace-exports/')&&r.request().method()==='GET'&&(await r.json()).cleanup==='released');
    await server.send('release-read');
    const result=await (await terminal).json();
    expect(['source_changed','unsafe_source']).toContain(result.reason);
    await expect(other.getByRole('button',{name:'下载 ZIP',exact:true})).toBeHidden();
    await other.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await expect(other.getByRole('button',{name:'下载 ZIP',exact:true})).toBeVisible();
    await other.getByRole('button',{name:'取消导出',exact:true}).click();
  } finally {await server.send('release-read');await other?.close();await server.close();}
});

test('SPEC03 AC24: accepted native transfer continues after leaving Run',async({page})=>{
  const server=await memoryServer({script:'tests/browser/trace_export_server.py'});
  try {
    await fixtureRun(page,server);
    await page.getByRole('combobox',{name:'导出模式'}).selectOption('diagnostic');
    await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toBeVisible();
    await server.send('hold-send');
    const download=await nativeZip(page);
    await server.send('await-send');
    await page.getByRole('link',{name:'运行',exact:true}).click();
    await expect(page.getByRole('region',{name:'追踪导出'})).toHaveCount(0);
    await server.send('release-send');
    zipManifest(await download.path(),'verified_complete');
  } finally {await server.send('release-send');await server.close();}
});

test('SPEC03 AC24: reconnect and restarted instance cannot restore old ready task',async({page})=>{
  const server=await memoryServer({script:'tests/browser/trace_export_server.py'});
  try {
    await fixtureRun(page,server);
    await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toBeVisible();
    await page.evaluate(()=>window.dispatchEvent(new Event('offline')));
    await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toBeHidden();
    await page.evaluate(()=>window.dispatchEvent(new Event('online')));
    await expect(page.getByRole('button',{name:'生成追踪导出',exact:true})).toBeEnabled();
    await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toBeVisible();
    await server.restart();
    await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toBeHidden();
    await expect(page.getByRole('button',{name:'生成追踪导出',exact:true})).toBeEnabled();
    await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toBeVisible();
    const download=await nativeZip(page);
    zipManifest(await download.path(),'verified_complete');
  } finally {await server.close();}
});

test('SPEC03 CE02: actual recording pending and failed then historical unknown remain distinct in browser',async({page})=>{
  const server=await memoryServer({script:'tests/browser/trace_export_server.py'});
  try {
    await server.send('recording-hold-fail');
    await fixtureRun(page,server,'recording result',false);
    await server.send('await-recording');
    const refused=page.waitForResponse(r=>r.url().endsWith('/api/trace-exports'));
    await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    expect((await (await refused).json()).code).toBe('not_stopped');
    await server.send('release-recording');
    await expect(page.getByText('保存失败',{exact:true}).first()).toBeVisible();
    await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await expect(page.getByRole('region',{name:'追踪导出'})).toContainText('known_incomplete');
    zipManifest(await (await nativeZip(page)).path(),'known_incomplete');
  } finally {await server.send('release-recording');await server.close();}
});

test('SPEC03 CE02: historical run without recording evidence stays unknown after restart',async({page})=>{
  const historic=await memoryServer({script:'tests/browser/trace_export_server.py'});
  try {
    await fixtureRun(page,historic);
    await historic.send('unknown-recording');
    await historic.restart();
    await page.reload();
    await expect(page.getByRole('button',{name:'生成追踪导出',exact:true})).toBeEnabled();
    await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
    await expect(page.getByRole('region',{name:'追踪导出'})).toContainText('unknown');
    zipManifest(await (await nativeZip(page)).path(),'unknown');
  } finally {await historic.close();}
});

test('SPEC03 AC24: cached page return rechecks instance and replaces disposed export view',async({page})=>{
  await createRun(page);
  await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
  await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toBeVisible();
  const cancelled=page.waitForResponse(r=>r.url().endsWith('/cancel')&&r.url().includes('/api/trace-exports/'));
  await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true})));
  expect((await (await cancelled).json()).cleanup).toBe('released');
  await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));
  await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toBeHidden();
  await expect(page.getByRole('button',{name:'生成追踪导出',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'生成追踪导出',exact:true}).click();
  await expect(page.getByRole('button',{name:'下载 ZIP',exact:true})).toBeVisible();
  zipManifest(await (await nativeZip(page)).path(),'verified_complete');
});
