import {test, expect} from '@playwright/test';
import {memoryServer} from './memory-server.js';

async function routingRun(page, server, message='解释图执行') {
  await page.goto(server.origin+'/behaviour');
  await page.getByRole('checkbox',{name:'启用消息分流'}).check();
  await page.getByRole('button',{name:'保存设置',exact:true}).click();
  await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
  const created=page.waitForResponse(r=>r.url().endsWith('/api/sessions')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  const session=(await (await created).json()).session_id;
  await expect.poll(()=>page.evaluate(()=>sessionStorage.getItem('alfred.session'))).toBe(session);
  if(await page.getByRole('button',{name:'展开对话',exact:true}).count()) await page.getByRole('button',{name:'展开对话',exact:true}).click();
  await page.getByRole('textbox',{name:'消息'}).fill(message);
  const accepted=page.waitForResponse(r=>r.url().endsWith('/api/runs')&&r.request().method()==='POST');
  await expect(page.getByRole('button',{name:'发送',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'发送',exact:true}).click();
  const response=await accepted;
  expect(response.status()).toBe(202);
  const identity=await response.json();
  await expect(page.locator('#messages').getByText('已保存',{exact:true})).toBeVisible();
  return identity.run_id;
}

test('AC-01/02/03 CE-10: actual routing path is collapsed and refresh is manual',async({page})=>{
  const server=await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    const run=await routingRun(page,server);
    let reads=0;page.on('request',r=>{if(r.url().includes('/api/run-path?'))reads++;});
    await page.goto(server.origin+'/runs/'+run);
    const button=page.getByRole('button',{name:'本次执行路径',exact:true});
    await expect(button).toHaveAttribute('aria-expanded','false');
    expect(reads).toBe(0);
    await button.click();
    const panel=page.getByRole('region',{name:'本次执行路径',exact:true});
    await expect(panel.locator('svg [data-node-id]')).toHaveCount(9);
    await expect(panel).toContainText('持久 trace');
    await expect(panel).toContainText('已提交');
    expect(reads).toBe(1);
  } finally {await server.close();}
});

const region=page=>page.getByRole('region',{name:'本次执行路径',exact:true});
const pathRoute='**/api/run-path?*';
function barrier(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
async function openPath(page,origin,run){
  await page.goto(origin+'/runs/'+run);
  await expect(page.getByRole('button',{name:'新建会话',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
  await expect(region(page).locator('svg [data-node-id]')).toHaveCount(9);
  return region(page);
}
async function rawPath(page,origin,run){return (await page.request.get(origin+'/api/run-path?run_id='+run)).json();}

for(const viewport of [{width:1280,height:850},{width:390,height:844}]) test(`CE-11/AC-19 keyboard graph/text equivalence ${viewport.width}`,async({page})=>{
  const server=await memoryServer({script:'tests/browser/routing_server.py'});
  try{
    const run=await routingRun(page,server);
    await page.setViewportSize(viewport);
    await page.goto(server.origin+'/runs/'+run);
    await expect(page.getByRole('button',{name:'新建会话',exact:true})).toBeEnabled();
    await page.keyboard.press('Escape');
    const toggle=page.getByRole('button',{name:'本次执行路径',exact:true});
    await toggle.focus();await page.keyboard.press('Enter');
    const panel=region(page);
    await expect(panel.locator('svg [data-node-id]')).toHaveCount(9);
    const snapshot=await rawPath(page,server.origin,run);
    expect(await panel.locator('svg [data-node-id]').evaluateAll(els=>els.map(e=>e.getAttribute('data-node-id')).sort())).toEqual(snapshot.nodes.map(n=>n.node_id).sort());
    expect(await panel.locator('svg [data-graph-edge]').evaluateAll(els=>els.map(e=>JSON.parse(e.getAttribute('data-graph-edge'))))).toEqual(snapshot.description.topology.edges);
    expect(await panel.locator('[data-topology-edge]').evaluateAll(els=>els.map(e=>JSON.parse(e.getAttribute('data-topology-edge'))))).toEqual(snapshot.description.topology.edges);
    const select=panel.getByRole('button',{name:/ · classify$/});
    await select.focus();await page.keyboard.press('Enter');
    await expect(panel.getByRole('region',{name:'节点详情'})).toHaveCount(0); // detail uses a labelled div
    await expect(panel.locator('[aria-label="节点详情"]')).toContainText('Step');
    await panel.getByRole('button',{name:'放大',exact:true}).focus();await page.keyboard.press('Enter');
    const reference=panel.locator('[aria-label="节点详情"] a').first();
    await expect(reference).toBeVisible();
    const target=await reference.getAttribute('href');
    await reference.focus();await page.keyboard.press('Enter');
    expect(await page.locator(target).count()).toBe(1);
    const view=await panel.locator('svg').getAttribute('viewBox');
    await panel.getByRole('button',{name:'重新读取',exact:true}).focus();await page.keyboard.press('Enter');
    await expect(panel).not.toContainText('重新读取中');
    expect(await panel.locator('svg').getAttribute('viewBox')).toBe(view);
    await expect(panel.locator('[aria-label="节点详情"]')).toContainText('classify');
    await panel.getByRole('button',{name:'收起执行路径',exact:true}).focus();await page.keyboard.press('Enter');
    await toggle.focus();await page.keyboard.press('Enter');
    await expect(panel.locator('svg')).toHaveAttribute('viewBox',view);
    await page.reload();await expect(toggle).toHaveAttribute('aria-expanded','false');
  }finally{await server.close();}
});

test('CE-08 latest request wins across reverse responses, reopen and Run navigation',async({page})=>{
  const server=await memoryServer({script:'tests/browser/routing_server.py'});
  try{
    const a=await routingRun(page,server), b=await routingRun(page,server,'不用回复');
    await openPath(page,server.origin,a);
    const held=barrier(),seen=barrier();let use=true;
    await page.route(pathRoute,async route=>{
      if(!use)return route.continue();use=false;
      const response=await route.fetch();seen.resolve();await held.promise;await route.fulfill({response});
    });
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();await seen.promise;
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(page)).not.toContainText('重新读取中');
    await region(page).getByRole('button',{name:'收起执行路径',exact:true}).click();
    await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
    await expect(region(page)).not.toContainText('重新读取中');
    await openPath(page,server.origin,b);
    await expect(region(page)).toContainText('NoAction');
    held.resolve();
    await expect(region(page)).toContainText('NoAction');
    await expect(region(page).locator('[aria-label="节点详情"]')).toBeEmpty();
    await page.unroute(pathRoute);
  }finally{await server.close();}
});

test('CE-01/08: real process restart reads P1 retained graph and invalidates old response',async({page})=>{
  const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  try{
    const run=await routingRun(page,server);
    const p1=await rawPath(page,server.origin,run);
    await openPath(page,server.origin,run);
    const held=barrier(),seen=barrier();let use=true;
    await page.route(pathRoute,async route=>{
      if(!use)return route.continue();use=false;
      const response=await route.fetch();seen.resolve();await held.promise;await route.fulfill({response});
    });
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();await seen.promise;
    await server.restart();
    await page.reload();
    await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
    await expect(region(page).locator('svg [data-node-id]')).toHaveCount(9);
    const p2=await rawPath(page,server.origin,run);
    expect(p2.identity).toEqual(p1.identity);
    expect(p2.service_instance_id).not.toBe(p1.service_instance_id);
    held.resolve();
    await region(page).getByText('快照身份与输入声明',{exact:true}).click();
    await expect(region(page)).toContainText(p2.service_instance_id);
    await expect(region(page)).toContainText(p1.identity.process_instance_id);
    await expect(region(page)).toContainText('持久 trace');
    await page.unroute(pathRoute);
  }finally{await server.close();}
});

test('CE-07/15 network failure keeps stale snapshot; verified missing clears it and recovery restores',async({page})=>{
  const {readdir,readFile,unlink,writeFile}=await import('node:fs/promises');
  const {join}=await import('node:path');
  const server=await memoryServer({script:'tests/browser/routing_server.py'});
  try{
    const run=await routingRun(page,server);
    await page.goto(server.origin+'/runs/'+run);
    await page.route(pathRoute,route=>route.abort());
    await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
    await expect(region(page)).toContainText('首次读取失败');
    await page.unroute(pathRoute);
    await region(page).getByRole('button',{name:'重试读取',exact:true}).click();
    await expect(region(page).locator('svg [data-node-id]')).toHaveCount(9);
    await page.route(pathRoute,route=>route.abort());
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(page)).toContainText('旧快照 / 本次读取失败');
    await expect(region(page).locator('svg [data-node-id]')).toHaveCount(9);
    await page.unroute(pathRoute);
    const files=await readdir(join(server.directory,'traces'),{recursive:true});
    const trace=join(server.directory,'traces',files.find(f=>f.endsWith('/trace.jsonl')));
    const bytes=await readFile(trace);await unlink(trace);
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(page)).toContainText('路径不可用。缺失');
    await expect(region(page).locator('svg')).toHaveCount(0);
    await writeFile(trace,bytes);
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(page).locator('svg [data-node-id]')).toHaveCount(9);
  }finally{await server.close();}
});

for(const failed of [false,true]) test(`CE-12 pending snapshot stays independent from recorded SSE ${failed}`,async({page})=>{
  const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  try{
    await page.goto(server.origin+'/behaviour');
    await page.getByRole('checkbox',{name:'启用消息分流'}).check();
    await page.getByRole('button',{name:'保存设置',exact:true}).click();
    await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
    const {csrf_token}=await (await page.request.get(server.origin+'/api/entry')).json();
    const headers={'x-agent-alfred-csrf':csrf_token};
    const {session_id}=await (await page.request.post(server.origin+'/api/sessions',{headers,data:{}})).json();
    await server.send('hold-recording');
    if(failed)await server.send('fail-recording');
    const accepted=await page.request.post(server.origin+'/api/runs',{headers,data:{session_id,message:'explain'}});
    expect(accepted.status()).toBe(202);const {run_id}=await accepted.json();
    await server.send('wait-recording');
    await page.goto(server.origin+'/runs/'+run_id);
    await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
    await expect(region(page)).toContainText('本次进程观测，尚未保证保存');
    const held=barrier(),seen=barrier();let use=true;let old;
    await page.route(pathRoute,async route=>{
      if(!use)return route.continue();use=false;
      const response=await route.fetch();old=await response.json();seen.resolve();await held.promise;await route.fulfill({response});
    });
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();await seen.promise;
    expect(old.run.recording_state).toBe('pending');
    await server.send('release-recording');
    await expect.poll(async()=> (await rawPath(page,server.origin,run_id)).run.recording_state).toBe(failed?'failed':'recorded');
    held.resolve();
    await expect(region(page)).not.toContainText('重新读取中');
    await expect(region(page)).toContainText('记录 pending');
    await expect(region(page)).toContainText('本次进程观测，尚未保证保存');
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(page)).toContainText(failed?'记录 failed':'记录 recorded');
    await expect(region(page)).toContainText(failed?'本次进程观测':'持久 trace');
    await page.unroute(pathRoute);
  }finally{await server.close();}
});

test('CE-03/10 actual aggregation lost 202 and pending recording observation do not resubmit',async({page,context})=>{
  const {api}=await import('./memory-server.js');
  const server=await memoryServer({script:'tests/browser/aggregation_server.py'});
  let observer;
  try{
    await page.goto(server.origin+'/behaviour');
    const other=await api(page.request,server.origin);
    await other.command({operation_id:'path-source',action:'save',kind:'semantic',payload:{subject:'me',fact:'coffee preference'}});
    const {csrf_token}=await (await page.request.get(server.origin+'/api/entry')).json();
    const {session_id}=await (await page.request.post(server.origin+'/api/sessions',{headers:{'x-agent-alfred-csrf':csrf_token},data:{}})).json();
    await page.reload();
    await page.getByRole('combobox',{name:'目标会话'}).selectOption(session_id);
    await page.getByRole('textbox',{name:'聚合目标',exact:true}).fill('coffee draft');
    await page.getByRole('textbox',{name:'聚合关键词',exact:true}).fill('coffee');
    await page.getByRole('checkbox',{name:'情景记忆',exact:true}).uncheck();
    await page.getByRole('checkbox',{name:'会话窗口',exact:true}).uncheck();
    await server.send('hold-model');await server.send('hold-recording');
    let posts=0;const accepted=barrier();let identity;
    await page.route('**/api/runs',async route=>{
      if(route.request().method()!=='POST')return route.continue();
      posts++;const response=await route.fetch();expect(response.status()).toBe(202);
      identity=await response.json();await route.abort();accepted.resolve();
    });
    await expect(page.getByRole('button',{name:'生成聚合草稿',exact:true})).toBeEnabled();
    await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();await accepted.promise;
    await server.send('wait-stream');
    await expect(page.getByText(/准入未确认；请查看已有运行/)).toBeVisible();
    async function account() {
      const snapshot=await (await page.request.post(server.origin+'/api/ops/snapshots',{headers:{'x-agent-alfred-csrf':csrf_token},data:{range:'7d',timezone:'UTC',run_id:identity.run_id}})).json();
      const detail=await (await page.request.get(server.origin+'/api/ops/detail?'+new URLSearchParams({snapshot_id:snapshot.snapshot_id,run_id:identity.run_id}))).json();
      return {summary:snapshot.summary,attempts:detail.run.attempts.map(a=>({attempt_id:a.attempt_id,outcome:a.outcome,usage:a.usage,cost:a.cost})),tools:detail.run.tools};
    }
    const beforeModel=await account();
    observer=await context.newPage();
    await observer.goto(server.origin+'/runs/'+identity.run_id);
    await observer.getByRole('button',{name:'本次执行路径',exact:true}).click();
    await expect(region(observer).locator('svg [data-node-id]')).toHaveCount(15);
    await expect(region(observer)).toContainText('disabled_by_config');
    await region(observer).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(page.getByRole('button',{name:'生成聚合草稿',exact:true})).toBeDisabled();
    expect(posts).toBe(1);
    expect(await account()).toEqual(beforeModel);
    await expect(page.getByRole('combobox',{name:'目标会话'})).toHaveValue(session_id);
    await expect(page.getByRole('textbox',{name:'聚合目标',exact:true})).toHaveValue('coffee draft');
    await server.send('release-model');
    await expect.poll(async()=> (await rawPath(observer,server.origin,identity.run_id)).run.outcome).toBe('completed');
    const beforePending=await account();
    await region(observer).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(observer)).toContainText('记录 pending');
    expect(await account()).toEqual(beforePending);
    await expect(page.getByRole('button',{name:'生成聚合草稿',exact:true})).toBeDisabled();
    await server.send('release-recording');
    await expect.poll(async()=> (await rawPath(observer,server.origin,identity.run_id)).run.recording_state).toBe('recorded');
    await region(observer).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(observer)).toContainText('持久 trace');
    expect(posts).toBe(1);
    const runs=await (await page.request.get(server.origin+'/api/runs')).json();
    expect(runs.runs).toHaveLength(1);
    const saved=await account();
    expect(saved.attempts).toHaveLength(1);
    expect(saved.tools).toHaveLength(1);
    await region(observer).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(observer)).not.toContainText('重新读取中');
    expect(await account()).toEqual(saved);
    const {instance_id}=await (await page.request.get(server.origin+'/api/entry')).json();
    const recovered=await (await page.request.get(server.origin+'/api/reply?'+new URLSearchParams({process_instance_id:instance_id,session_id,run_id:identity.run_id}))).json();
    expect(recovered.reply_text).toBe('已验证草稿 [[S1]]');

  }finally{await observer?.close();await server.close();}
});

async function admitRouting(page,origin){
  const {csrf_token}=await (await page.request.get(origin+'/api/entry')).json();
  const headers={'x-agent-alfred-csrf':csrf_token};
  const state=await (await page.request.get(origin+'/api/behaviour')).json();
  expect((await page.request.post(origin+'/api/behaviour',{headers,data:{action:'save',enabled:true,expected_revision:state.revision}})).status()).toBe(200);
  const {session_id}=await (await page.request.post(origin+'/api/sessions',{headers,data:{}})).json();
  const response=await page.request.post(origin+'/api/runs',{headers,data:{session_id,message:'explain'}});
  expect(response.status()).toBe(202);return response.json();
}

test('CE-04 half-wave HTTP and retained prefix never display whole-wave commit',async({page})=>{
  const {readdir,readFile,writeFile}=await import('node:fs/promises');const {join}=await import('node:path');
  const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  try{
    await server.send('hold-wave');
    const {run_id}=await admitRouting(page,server.origin);
    await server.send('wait-wave');
    await page.goto(server.origin+'/runs/'+run_id);
    await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
    await expect(region(page)).toContainText('计算成功 · 尚未证明波提交');
    await expect(region(page)).toContainText('Wave 0 · 未知');
    await server.send('release-wave');
    await expect.poll(async()=> (await rawPath(page,server.origin,run_id)).source).toBe('trace');
    await expect(region(page)).toContainText('Wave 0 · 未知');
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(page)).toContainText('Wave 0 · 已提交');
    const files=await readdir(join(server.directory,'traces'),{recursive:true});
    const trace=join(server.directory,'traces',files.find(f=>f.endsWith('/trace.jsonl')));
    const lines=(await readFile(trace,'utf8')).trimEnd().split('\n');
    await writeFile(trace,lines.slice(0,lines.findIndex(l=>JSON.parse(l).payload_name==='path.wave')).join('\n')+'\n');
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(page)).toContainText('路径 partial');
    await expect(region(page)).toContainText('Wave 0 · 未知');
    await expect(region(page)).toContainText('计算成功 · 尚未证明波提交');
  }finally{await server.close();}
});

for(const mode of ['abort','unknown'])test(`CE-02/11 real test graph ${mode} state and plain text`,async({page})=>{
  const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  try{
    await server.send('graph '+mode);
    const run=await routingRun(page,server);
    await page.goto(server.origin+'/runs/'+run);
    await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
    await expect(region(page)).toContainText('<img src=x onerror=alert(1)> literal explanation');
    await expect(region(page).locator('img')).toHaveCount(0);
    await expect(region(page)).toContainText('暂无说明');
    if(mode==='abort'){
      await expect(region(page)).toContainText('Graph Failed');
      await expect(region(page)).toContainText('图外回退 · 已进入');
      await expect(region(page)).toContainText('Wave 0 · 已提交');
      await expect(region(page)).toContainText('Wave 1 · 撤销');
      await expect(region(page)).toContainText('未开始（本次证据范围）');
    }else{
      await expect(region(page).locator('svg [data-node-id]')).toHaveCount(1);
      await expect(region(page)).toContainText('unknown_valid');
    }
  }finally{await server.close();}
});

for(const mode of ['complete','prefix','missing','recording_failed'])test(`CE-06 real SIGKILL restart browser ${mode}`,async({page})=>{
  const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  try{
    await server.send('hold-recording');
    if(['prefix','missing'].includes(mode))await server.send('hold-trace '+mode);
    if(mode==='recording_failed')await server.send('fail-recording');
    const {run_id}=await admitRouting(page,server.origin);
    await server.send(['prefix','missing'].includes(mode)?'wait-trace':'wait-recording');
    await page.goto(server.origin+'/runs/'+run_id);
    await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
    await expect(region(page)).toContainText('本次进程观测，尚未保证保存');
    if(mode==='recording_failed'){
      await server.send('release-recording');
      await expect.poll(async()=> (await rawPath(page,server.origin,run_id)).run.recording_state).toBe('failed');
      await server.send('repair-recording');
    }
    await server.crashAndRestart();
    await page.reload();
    await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
    if(mode==='missing'){
      await expect(region(page)).toContainText('缺少历史结构');
      await expect(region(page).locator('svg')).toHaveCount(0);
    }else{
      await expect(region(page)).toContainText('持久 trace');
      await expect(region(page)).toContainText('Run interrupted');
      await expect(region(page)).toContainText(mode==='prefix'?'路径 partial':'路径 available');
    }
    await expect(region(page)).not.toContainText('本次进程观测，尚未保证保存');
  }finally{await server.close();}
});

for(const damage of ['prune','corrupt','version','legacy','too_large','seq','wrong_run'])test(`CE-07 real trace refresh ${damage} replaces old path`,async({page})=>{
  const {readdir,readFile,writeFile,truncate}=await import('node:fs/promises');const {join}=await import('node:path');
  const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  try{
    const run=await routingRun(page,server);await openPath(page,server.origin,run);
    const files=await readdir(join(server.directory,'traces'),{recursive:true});
    const trace=join(server.directory,'traces',files.find(f=>f.endsWith('/trace.jsonl')));
    const bytes=await readFile(trace);const events=bytes.toString().trimEnd().split('\n').map(JSON.parse);
    const captured=events.find(e=>e.payload_name==='path.captured');
    if(damage==='prune')await server.send('prune');
    else if(damage==='too_large')await truncate(trace,32*1024*1024+1);
    else if(damage==='corrupt')await writeFile(trace,'not-json\n'+bytes);
    else{
      if(damage==='version')captured.payload.evidence_version=999;
      if(damage==='legacy')events.splice(events.indexOf(captured),1);
      if(damage==='seq')captured.seq=0;
      if(damage==='wrong_run')captured.run_id='another-run';
      await writeFile(trace,events.map(e=>JSON.stringify(e)+'\n').join(''));
    }
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
    const reason={prune:'已裁剪',corrupt:'损坏',version:'不支持',legacy:'缺少历史结构',too_large:'32 MiB',seq:'损坏',wrong_run:'损坏'}[damage];
    await expect(region(page)).toContainText(reason);
    await expect(region(page).locator('svg')).toHaveCount(0);
    await expect(region(page)).toContainText('独立业务摘要');
    if(damage!=='prune'){
      await writeFile(trace,bytes);await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
      await expect(region(page).locator('svg [data-node-id]')).toHaveCount(9);
    }
  }finally{await server.close();}
});

test('CE-09 current cache overflow is unavailable while real business completes',async({page})=>{
  const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  try{
    await server.send('limit');await server.send('hold-recording');
    const {run_id}=await admitRouting(page,server.origin);await server.send('wait-recording');
    await page.goto(server.origin+'/runs/'+run_id);await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
    await expect(region(page)).toContainText('超过当前观测缓存上限');
    await expect(region(page).locator('svg')).toHaveCount(0);
    const current=await rawPath(page,server.origin,run_id);
    expect(current.run).toMatchObject({outcome:'completed',recording_state:'pending'});
    await server.send('release-recording');
    await expect.poll(async()=> (await rawPath(page,server.origin,run_id)).source).toBe('trace');
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(page).locator('svg [data-node-id]')).toHaveCount(9);
  }finally{await server.close();}
});

test('CE-08 same Run reverse responses preserve latest durable snapshot',async({page})=>{
  const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  const held=barrier(),seen=barrier(),delivered=barrier();let use=true;
  try{
    await server.send('hold-recording');const {run_id}=await admitRouting(page,server.origin);await server.send('wait-recording');
    await page.goto(server.origin+'/runs/'+run_id);
    await page.route(pathRoute,async route=>{
      if(!use)return route.continue();use=false;
      const response=await route.fetch();expect((await response.json()).source).toBe('process');
      seen.resolve();await held.promise;await route.fulfill({response});delivered.resolve();
    });
    await page.getByRole('button',{name:'本次执行路径',exact:true}).click();await seen.promise;
    await server.send('release-recording');
    await expect.poll(async()=> (await rawPath(page,server.origin,run_id)).source).toBe('trace');
    await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region(page)).toContainText('持久 trace');
    held.resolve();await delivered.promise;
    await expect(region(page)).toContainText('持久 trace');
    await expect(region(page)).not.toContainText('本次进程观测');
    await page.unroute(pathRoute);
  }finally{held.resolve();await server.close();}
});


for(const mode of ['fallback','recovery','failure','blocked','bypass','unavailable']) test(`CE-05/10 real routing browser ${mode}`,async({page})=>{
  const server=await memoryServer({script:'tests/browser/run_path_server.py',prepare:mode==='unavailable'?async directory=>{
    const {writeFile}=await import('node:fs/promises');await writeFile(directory+'/graph-unavailable','fault');
  }:undefined});
  try{
    if(mode==='recovery') {
      const {writeFile}=await import('node:fs/promises');
      await writeFile(server.directory+'/corrupt-context','fault');
    }
    const {csrf_token}=await (await page.request.get(server.origin+'/api/entry')).json();
    const headers={'x-agent-alfred-csrf':csrf_token};
    const current=await (await page.request.get(server.origin+'/api/behaviour')).json();
    expect((await page.request.post(server.origin+'/api/behaviour',{headers,data:{action:'save',enabled:mode!=='bypass',expected_revision:current.revision}})).status()).toBe(200);
    const message={fallback:'STATS graph fallback',recovery:'你好',failure:'STATS full failure',blocked:'PATH side effect',bypass:'ordinary',unavailable:'ordinary'}[mode];
    const {session_id}=await (await page.request.post(server.origin+'/api/sessions',{headers,data:{}})).json();
    const response=await page.request.post(server.origin+'/api/runs',{headers,data:{message,session_id}});
    expect(response.status()).toBe(202);const {run_id}=await response.json();
    await expect.poll(async()=> (await rawPath(page,server.origin,run_id)).run.recording_state).toBe('recorded');
    await page.goto(server.origin+'/runs/'+run_id);
    await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
    const panel=region(page), path=await rawPath(page,server.origin,run_id);
    if(['bypass','unavailable'].includes(mode)) {await expect(panel).toContainText(mode==='bypass'?'disabled_by_config':'routing_unavailable');expect(path.reason).toBe('graph_bypassed');expect(path.stages[0].entered).toBe(true);expect(path.run.outcome).toBe('completed');}
    else {
      await expect(panel.locator('svg [data-node-id]')).toHaveCount(9);
      if(mode==='fallback') {expect(path.edges.filter(e=>e.state==='taken').some(e=>e.label==='fallback')).toBe(true);await expect(panel).toContainText('fallback');}
      if(mode==='unavailable') await server.send('graph unavailable');
    if(mode==='recovery') {expect(path.graph_outcome).toBe('CompletedWithRecovery');expect(path.nodes.find(n=>n.node_id==='project_context').state).toBe('failed');expect(path.edges.find(e=>e.kind==='error').state).toBe('taken');await expect(panel).toContainText('CompletedWithRecovery');}
      if(['failure','blocked'].includes(mode)) {
        expect(path.graph_outcome).toBe('Failed');expect(path.run.outcome).toBe(mode==='failure'?'completed':'failed');
        expect(path.stages[0].entered).toBe(mode==='failure');await expect(panel).toContainText(mode==='failure'?'图外回退 · 已进入':'图外回退 · 已阻止');
        if(mode==='blocked')await expect(panel).toContainText('side_effect_occurred');
      }
    }
  }finally{await server.close();}
});


for(const fault of ['future_started','future_finished','future_skipped','future_aborted','not_taken_target','unknown_route_label','run_finished_before_graph','contradictory_not_started','unknown_graph_outcome','premature_wave_commit'])test(`C2 STD-01 SPEC-01 COORD-01 protocol ${fault}`,async({page})=>{
  const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  try{
    const run=await routingRun(page,server);
    const panel=await openPath(page,server.origin,run);
    const before=await rawPath(page,server.origin,run);
    const runsBefore=await (await page.request.get(server.origin+'/api/runs')).json();
    await server.send('damage '+fault);
    await panel.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(panel).toContainText('路径不可用');
    await expect(panel.locator('svg')).toHaveCount(0);
    const bad=await rawPath(page,server.origin,run);
    expect(bad.reason).toBe('corrupt');expect(bad.summary).toEqual(before.summary);expect(bad.run).toEqual(before.run);
    await server.send('restore-trace');
    await panel.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(panel.locator('svg [data-node-id]')).toHaveCount(9);
    const restored=await rawPath(page,server.origin,run);
    expect(restored.nodes).toEqual(before.nodes);expect(restored.edges).toEqual(before.edges);
    expect(await (await page.request.get(server.origin+'/api/runs')).json()).toEqual(runsBefore);
  }finally{await server.close();}
});


for(const fault of ['fallback_before_graph','bypass_after_graph','unknown_stage','nonboolean_entered','stage_after_run','bypass_before_graph','unknown_reason','duplicate_stage','fallback_without_graph','nocapture_after_run','nocapture_unknown_stage','nocapture_nonboolean','nocapture_duplicate'])test(`C3 STD-02 SPEC-02 stage ${fault}`,async({page})=>{
  const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  try{
    let run,panel;
    if(fault.startsWith('nocapture_')) {
      const {csrf_token}=await (await page.request.get(server.origin+'/api/entry')).json();
      const headers={'x-agent-alfred-csrf':csrf_token};
      const {session_id}=await (await page.request.post(server.origin+'/api/sessions',{headers,data:{}})).json();
      const response=await page.request.post(server.origin+'/api/runs',{headers,data:{session_id,message:'ordinary'}});
      expect(response.status()).toBe(202);run=(await response.json()).run_id;
      await expect.poll(async()=> (await rawPath(page,server.origin,run)).run.recording_state).toBe('recorded');
      await page.goto(server.origin+'/runs/'+run);await page.getByRole('button',{name:'本次执行路径',exact:true}).click();panel=region(page);
      await expect(panel).toContainText('disabled_by_config');
    } else {run=await routingRun(page,server);panel=await openPath(page,server.origin,run);}
    const before=await rawPath(page,server.origin,run);
    const runs=await (await page.request.get(server.origin+'/api/runs')).json();
    await server.send('stage-damage '+fault);
    await panel.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(panel).toContainText('路径不可用');await expect(panel.locator('svg')).toHaveCount(0);
    const bad=await rawPath(page,server.origin,run);
    expect(bad.reason).toBe('corrupt');expect(bad.stages).toEqual([]);expect(bad.summary).toEqual(before.summary);
    await server.send('restore-trace');await panel.getByRole('button',{name:'重新读取',exact:true}).click();
    if(fault.startsWith('nocapture_')) await expect(panel).toContainText('disabled_by_config');
    else await expect(panel.locator('svg [data-node-id]')).toHaveCount(9);
    expect((await rawPath(page,server.origin,run)).nodes).toEqual(before.nodes);
    expect(await (await page.request.get(server.origin+'/api/runs')).json()).toEqual(runs);
  }finally{await server.close();}
});

for(const fault of ['terminal_count','completed_failure_reason','complete_node_failed','complete_budget','aggregation_fallback','aggregation_bypass'])test(`C4 SPEC-03 COORD-02 real trace rejection and recovery ${fault}`,async({page})=>{
  const {readdir,readFile,writeFile}=await import('node:fs/promises');const {join}=await import('node:path');
  const aggregation=fault.startsWith('aggregation_');
  const server=await memoryServer({script:aggregation?'tests/browser/aggregation_server.py':'tests/browser/run_path_server.py'});
  try{
    let run;
    if(aggregation){
      const {api}=await import('./memory-server.js');const client=await api(page.request,server.origin);
      expect((await client.command({operation_id:'c4-seed',kind:'semantic',action:'save',payload:{subject:'me',fact:'coffee source'}})).status).toBe(200);
      await server.send('fail-model');
      const {csrf_token}=await (await page.request.get(server.origin+'/api/entry')).json();const headers={'x-agent-alfred-csrf':csrf_token};
      const {session_id}=await (await page.request.post(server.origin+'/api/sessions',{headers,data:{}})).json();
      const response=await page.request.post(server.origin+'/api/runs',{headers,data:{purpose:'aggregation',session_id,message:'coffee',keywords:'coffee',sources:['semantic']}});
      expect(response.status()).toBe(202);run=(await response.json()).run_id;
      await expect.poll(async()=> (await rawPath(page,server.origin,run)).run.recording_state).toBe('recorded');
      await page.goto(server.origin+'/runs/'+run);await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
      await expect(region(page).locator('svg [data-node-id]')).toHaveCount(15);
    }else{run=await routingRun(page,server);await openPath(page,server.origin,run);}
    const panel=region(page),before=await rawPath(page,server.origin,run);
    const runs=await (await page.request.get(server.origin+'/api/runs')).json();
    async function account(){
      const {csrf_token}=await (await page.request.get(server.origin+'/api/entry')).json();
      const response=await page.request.post(server.origin+'/api/ops/snapshots',{headers:{'x-agent-alfred-csrf':csrf_token},data:{range:'7d',timezone:'UTC',run_id:run}});expect(response.status()).toBe(200);
      const snapshot=await response.json();const detailResponse=await page.request.get(server.origin+'/api/ops/detail?'+new URLSearchParams({snapshot_id:snapshot.snapshot_id,run_id:run}));expect(detailResponse.status()).toBe(200);
      const detail=await detailResponse.json();return {summary:snapshot.summary,attempts:detail.run.attempts.map(a=>({attempt_id:a.attempt_id,outcome:a.outcome,usage:a.usage,cost:a.cost})),tools:detail.run.tools};
    }
    const ops=await account();
    const files=await readdir(join(server.directory,'traces'),{recursive:true});const trace=join(server.directory,'traces',files.find(f=>f.endsWith('/trace.jsonl')));
    const original=await readFile(trace);let events=original.toString().trimEnd().split('\n').map(JSON.parse);
    const index=events.findIndex(e=>e.payload_name==='graph.finished');
    if(aggregation){
      expect(before.graph_outcome).toBe('Failed');expect(before.stages).toEqual([]);
      const stage=structuredClone(events[index]);stage.payload_name='path.stage';stage.payload={name:'path.stage',trace_policy:'persist',evidence_version:1,stage:'fallback',reason:'graph_failed',entered:true};
      events.splice(index+1,0,stage);
      if(fault==='aggregation_bypass'){
        stage.payload.stage='bypass';stage.payload.reason='disabled_by_config';
        events=events.filter(e=>!e.payload_name.startsWith('graph.')&&!e.payload_name.startsWith('node.')&&!['path.captured','path.wave'].includes(e.payload_name));
      }
      events.forEach((e,i)=>e.seq=i+1);
    }else Object.assign(events[index].payload,{outcome:fault==='complete_budget'?'BudgetExhausted':fault==='completed_failure_reason'?'Completed':'Failed',reason:{terminal_count:'terminal_count',completed_failure_reason:'terminal_count',complete_node_failed:'node_failed',complete_budget:'budget_exhausted'}[fault]});
    await writeFile(trace,events.map(e=>JSON.stringify(e)+'\n').join(''));
    await panel.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(panel).toContainText('路径不可用');await expect(panel.locator('svg')).toHaveCount(0);await expect(panel).toContainText('独立业务摘要');
    const bad=await rawPath(page,server.origin,run);expect(bad.reason).toBe('corrupt');expect(bad.stages).toEqual([]);expect(bad.summary).toEqual(before.summary);expect(bad.run).toEqual(before.run);
    await writeFile(trace,original);await panel.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(panel.locator('svg [data-node-id]')).toHaveCount(aggregation?15:9);
    const restored=await rawPath(page,server.origin,run);expect(restored.nodes).toEqual(before.nodes);expect(restored.edges).toEqual(before.edges);expect(restored.stages).toEqual(before.stages);
    expect(await (await page.request.get(server.origin+'/api/runs')).json()).toEqual(runs);
    expect(await account()).toEqual(ops);
  }finally{await server.close();}
});

for(const count of [0,1,2])test(`C4 SPEC-03 real engine legal terminal count ${count}`,async({page})=>{
  const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  try{
    await server.send('graph terminal'+count);
    const {run_id}=await admitRouting(page,server.origin);
    await expect.poll(async()=> (await rawPath(page,server.origin,run_id)).run.recording_state).toBe('recorded');
    await page.goto(server.origin+'/runs/'+run_id);await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
    await expect(region(page).locator('svg [data-node-id]')).toHaveCount(2);
    const path=await rawPath(page,server.origin,run_id);expect(path.status).toBe('available');
    expect(path.graph_outcome).toBe(count===1?'NoAction':'Failed');expect(path.graph_reason).toBe(count===1?'a':'terminal_count');
    expect(path.nodes.filter(n=>n.state==='succeeded')).toHaveLength(count);expect(path.waves.every(w=>w.state==='committed')).toBe(true);
    expect(path.run.outcome).toBe('completed');
    if(count!==1)await expect(region(page)).toContainText('图外回退 · 已进入');
  }finally{await server.close();}
});

for(const mode of ['overall_deadline','budget_exhausted','forced_stop','context_invalid','side_effect_unknown','side_effect_occurred','allowed_error_code'])test(`C5 SPEC-04 actual policy decision ${mode}`,async({page})=>{
  const {readdir,readFile,writeFile}=await import('node:fs/promises');const {join}=await import('node:path');
  const production=['overall_deadline','budget_exhausted','side_effect_occurred'].includes(mode);
  const allowed=mode==='allowed_error_code';
  const server=await memoryServer({script:'tests/browser/run_path_server.py',prepare:['overall_deadline','budget_exhausted'].includes(mode)?async directory=>{await writeFile(join(directory,'policy-case'),mode);}:undefined});
  try{
    if(!production)await server.send('graph eligibility-'+mode);
    await server.send('hold-recording');
    const {csrf_token}=await (await page.request.get(server.origin+'/api/entry')).json();const headers={'x-agent-alfred-csrf':csrf_token};
    const settings=await (await page.request.get(server.origin+'/api/behaviour')).json();expect((await page.request.post(server.origin+'/api/behaviour',{headers,data:{action:'save',enabled:true,expected_revision:settings.revision}})).status()).toBe(200);
    const {session_id}=await (await page.request.post(server.origin+'/api/sessions',{headers,data:{}})).json();
    const response=await page.request.post(server.origin+'/api/runs',{headers,data:{session_id,message:mode==='side_effect_occurred'?'PATH side effect':'explain'}});expect(response.status()).toBe(202);const {run_id}=await response.json();
    await server.send('wait-recording');
    const current=await rawPath(page,server.origin,run_id);expect(current.source).toBe('process');expect(current.status).toBe('available');
    expect(current.stages).toEqual([{stage:'fallback',reason:allowed?'graph_failed':mode,entered:allowed}]);
    await server.send('release-recording');await expect.poll(async()=> (await rawPath(page,server.origin,run_id)).run.recording_state).toBe('recorded');
    await page.goto(server.origin+'/runs/'+run_id);await page.getByRole('button',{name:'本次执行路径',exact:true}).click();const panel=region(page);
    await expect(panel.locator('svg [data-node-id]')).toHaveCount(production?9:1);await expect(panel).toContainText(allowed?'图外回退 · 已进入':'图外回退 · 已阻止');
    const before=await rawPath(page,server.origin,run_id);expect(before.stages).toEqual(current.stages);
    async function account(){
      const res=await page.request.post(server.origin+'/api/ops/snapshots',{headers,data:{range:'7d',timezone:'UTC',run_id}});expect(res.status()).toBe(200);const snapshot=await res.json();
      const detailResponse=await page.request.get(server.origin+'/api/ops/detail?'+new URLSearchParams({snapshot_id:snapshot.snapshot_id,run_id}));expect(detailResponse.status()).toBe(200);const detail=await detailResponse.json();
      return {summary:snapshot.summary,attempts:detail.run.attempts.map(a=>({attempt_id:a.attempt_id,outcome:a.outcome,usage:a.usage,cost:a.cost})),tools:detail.run.tools};
    }
    const ops=await account(),runs=await (await page.request.get(server.origin+'/api/runs')).json();
    const files=await readdir(join(server.directory,'traces'),{recursive:true});const trace=join(server.directory,'traces',files.find(f=>f.endsWith('/trace.jsonl')));const bytes=await readFile(trace);const events=bytes.toString().trimEnd().split('\n').map(JSON.parse);
    const stage=events.find(e=>e.payload_name==='path.stage');
    // Preserve the real adapter decision and all Graph facts; inject only the contradictory display claim.
    Object.assign(stage.payload,{entered:!allowed,reason:allowed?'forced_stop':'graph_failed'});
    await writeFile(trace,events.map(e=>JSON.stringify(e)+'\n').join(''));await panel.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(panel).toContainText('路径不可用');await expect(panel.locator('svg')).toHaveCount(0);await expect(panel).toContainText('独立业务摘要');
    const bad=await rawPath(page,server.origin,run_id);expect(bad.reason).toBe('corrupt');expect(bad.stages).toEqual([]);expect(bad.summary).toEqual(before.summary);expect(bad.run).toEqual(before.run);
    await writeFile(trace,bytes);await panel.getByRole('button',{name:'重新读取',exact:true}).click();await expect(panel.locator('svg [data-node-id]')).toHaveCount(production?9:1);
    const restored=await rawPath(page,server.origin,run_id);expect(restored.nodes).toEqual(before.nodes);expect(restored.edges).toEqual(before.edges);expect(restored.stages).toEqual(before.stages);
    expect(await account()).toEqual(ops);expect(await (await page.request.get(server.origin+'/api/runs')).json()).toEqual(runs);
  }finally{await server.close();}
});
