import {test, expect} from '@playwright/test';
import {memoryServer, api} from './memory-server.js';

test('aggregation CE-02/04/14: Behaviour to same Session, refresh and restart', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin + '/behaviour');
    await page.getByRole('button',{name:'新建会话',exact:true}).click();
    await page.getByRole('button',{name:'展开对话',exact:true}).click();
    await page.getByRole('textbox',{name:'消息',exact:true}).fill('hello history');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await expect(page.locator('#messages').getByText('离线路由回复')).toBeVisible();
    await page.getByRole('button',{name:'刷新会话',exact:true}).click();
    const session = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
    await page.getByRole('combobox',{name:'目标会话'}).selectOption(session);
    await page.getByRole('textbox',{name:'聚合目标',exact:true}).fill('整理本会话');
    await page.getByRole('textbox',{name:'聚合关键词',exact:true}).fill('hello');
    await page.getByRole('checkbox',{name:'语义记忆',exact:true}).uncheck();
    await page.getByRole('checkbox',{name:'情景记忆',exact:true}).uncheck();
    await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
    await expect(page.locator('#messages').getByRole('heading',{name:'聚合草稿'})).toBeVisible();
    await expect(page.locator('#messages').getByText('会话窗口 H1',{exact:true})).toBeVisible();
    await page.reload();
    await expect(page.locator('#messages').getByRole('heading',{name:'聚合草稿'})).toBeVisible();
    await server.restart();
    await page.reload();
    await expect(page.locator('#messages').getByRole('heading',{name:'聚合草稿'})).toBeVisible();
    const form = page.getByRole('region',{name:'手动聚合'});
    await form.getByRole('combobox',{name:'目标会话'}).selectOption(session);
    await form.getByRole('textbox',{name:'聚合目标',exact:true}).fill('无来源');
    for (const name of ['语义记忆','情景记忆','会话窗口']) await form.getByRole('checkbox',{name,exact:true}).uncheck();
    await form.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
    await expect(page.locator('#messages').getByText('未选择来源',{exact:true})).toBeVisible();
    const other = await api(page.request,server.origin);
    const runs = await other.get('/api/runs?filter=chat&limit=25');
    expect(runs.body.runs.filter(r => r.purpose === 'aggregation')).toHaveLength(2);
  } finally { await server.close(); }
});

async function acceptedRun(response, payload) {
  const body = await response.json();
  expect(response.status(), JSON.stringify({body, payload})).toBe(202);
  expect(body).toMatchObject({run_id:expect.any(String), session_id:expect.any(String)});
  return body;
}

async function prepareDraft(page, server, beforeCreate = async () => {}) {
  await page.goto(server.origin + '/behaviour');
  const other = await api(page.request, server.origin);
  const saved = await other.command({operation_id:'seed',kind:'semantic',action:'save',payload:{subject:'me',fact:'coffee source'}});
  expect(saved.status).toBe(200);
  await beforeCreate();
  const created = page.waitForResponse(r => r.url().endsWith('/api/sessions') && r.request().method() === 'POST');
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  const response = await created;
  const body = await response.json();
  expect(response.status(), JSON.stringify(body)).toBe(201);
  expect(body.session_id).toEqual(expect.any(String));
  const session = body.session_id;
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('alfred.session'))).toBe(session);
  await page.getByRole('button',{name:'展开对话',exact:true}).click();
  await page.getByRole('button',{name:'刷新会话',exact:true}).click();
  await page.getByRole('combobox',{name:'目标会话'}).selectOption(session);
  await page.getByRole('textbox',{name:'聚合目标',exact:true}).fill('draft goal');
  await page.getByRole('textbox',{name:'聚合关键词',exact:true}).fill('coffee');
  await page.getByRole('checkbox',{name:'情景记忆',exact:true}).uncheck();
  await page.getByRole('checkbox',{name:'会话窗口',exact:true}).uncheck();
  return {other, session, memoryId:saved.body.result.memory_id};
}

test('aggregation CE-08/11: candidate hidden, recording pending busy and failed recovery', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/aggregation_server.py'});
  try {
    const {other, session} = await prepareDraft(page,server);
    await server.send('hold-model');
    await server.send('hold-recording');
    await server.send('fail-recording');
    const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
    await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
    const response = await accepted;
    expect(response.status()).toBe(202);
    const {run_id} = await response.json();
    await expect(page.getByRole('button',{name:'生成聚合草稿',exact:true})).toBeDisabled();
    await expect(page.locator('#messages')).not.toContainText('不得展示的候选流');
    const mutation = await other.command({operation_id:'busy',kind:'semantic',action:'save',payload:{subject:'x',fact:'y'}});
    expect(mutation.status).toBe(409);
    await server.send('release-model');
    await expect(page.locator('#messages').getByText('已验证草稿 [[S1]]',{exact:true})).toBeVisible();
    const {csrf_token} = await (await page.request.get(server.origin+'/api/entry')).json();
    const again = await page.request.post(server.origin+'/api/runs',{headers:{'x-agent-alfred-csrf':csrf_token},data:{purpose:'aggregation',session_id:session,message:'again',keywords:'coffee',sources:['semantic']}});
    expect(again.status()).toBe(409);
    await server.send('release-recording');
    await expect(page.getByText('草稿未保存，请查看运行记录状态。')).toBeVisible();
    await page.reload();
    await expect(page.locator('#messages').getByText('已验证草稿 [[S1]]',{exact:true})).toBeVisible();
    await expect(page.locator('#messages').getByRole('heading',{name:'聚合草稿'})).toBeVisible();
    await server.send('repair-recording');
    await server.restart();
    await page.reload();
    await expect(page.locator('#messages').getByText('已验证草稿 [[S1]]',{exact:true})).toHaveCount(0);
    const runs = await (await api(page.request,server.origin)).get('/api/runs?filter=chat&limit=25');
    expect(runs.body.runs.find(r => r.run_id === run_id).outcome).toBe('interrupted');
  } finally { await server.close(); }
});

test('aggregation CE-08: dropped accepted response does not resubmit or move Session', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/aggregation_server.py'});
  try {
    const {session} = await prepareDraft(page,server);
    await page.getByRole('button',{name:'新建会话',exact:true}).click();
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem('alfred.session'))).not.toBe(session);
    const second = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
    expect(second).not.toBe(session);
    let sent = 0;
    await page.route('**/api/runs', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      sent++;
      const response = await route.fetch();
      await acceptedRun(response, route.request().postDataJSON());
      await route.abort();
    });
    await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
    await expect(page.getByText(/准入未确认；请查看已有运行/)).toBeVisible();
    await expect(page.locator('#messages').getByText('已验证草稿 [[S1]]',{exact:true})).toHaveCount(0);
    await page.reload();
    const other = await api(page.request,server.origin);
    const runs = await other.get('/api/runs?filter=chat&limit=25');
    const drafts = runs.body.runs.filter(r => r.purpose === 'aggregation');
    expect(drafts).toHaveLength(1);
    expect(drafts[0].session_id).toBe(session);
    expect(sent).toBe(1);
  } finally { await server.close(); }
});


test('aggregation CE-11: invalid streamed candidate never becomes a draft', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/aggregation_server.py'});
  try {
    const {other} = await prepareDraft(page,server);
    await server.send('invalid-model');
    await server.send('hold-model');
    const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
    await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
    const {run_id} = await acceptedRun(await accepted);
    await expect(page.locator('#messages')).not.toContainText('不得展示的候选流');
    await server.send('release-model');
    await expect.poll(async () => (await other.get('/api/run-evidence?run_id='+run_id)).body.memory?.aggregation?.error).toBe('invalid_draft_reference');
    await page.reload();
    await expect(page.locator('#messages')).not.toContainText('无效候选');
    await expect(page.locator('#messages')).not.toContainText('不得展示的候选流');
    await page.screenshot({path:'tmp/agent-work/issue-27/behaviour.png',fullPage:true});
  } finally { await server.close(); }
});


test('aggregation CE-12: real HTTP forgetting keeps draft and disables source', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/aggregation_server.py'});
  try {
    const {other, memoryId} = await prepareDraft(page,server);
    await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
    await expect(page.locator('#messages').getByText('已验证草稿 [[S1]]',{exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'生成聚合草稿',exact:true})).toBeEnabled();
    const gone = await other.command({operation_id:'delete-source',kind:'semantic',action:'delete',expected_version:1,payload:{id:memoryId}});
    expect(gone.status).toBe(200);
    expect(gone.body.result.status).toBe('deleted');
    expect(gone.body.forgetting.state).toBe('complete');
    await page.reload();
    await expect(page.locator('#messages').getByText('已验证草稿 [[S1]]',{exact:true})).toBeVisible();
    await expect(page.locator('#messages').getByText('语义记忆 S1 · 不可用',{exact:true})).toBeVisible();
    await expect(page.locator('#messages')).not.toContainText('coffee source');
  } finally { await server.close(); }
});


test('aggregation CE-14: every source combination through Behaviour', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    const {other} = await prepareDraft(page,server);
    const episode = await other.command({operation_id:'episode',kind:'episodic',action:'save',payload:{summary:'coffee episode',occurred_at:'2026-09-14T10:00:00+08:00',occurred_until:null}});
    expect(episode.status).toBe(200);
    await page.getByRole('textbox',{name:'消息',exact:true}).fill('history coffee');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await expect(page.locator('#messages').getByText('离线路由回复')).toBeVisible();
    const names = ['语义记忆','情景记忆','会话窗口'];
    for (let bits=1;bits<8;bits++) {
      await expect(page.getByRole('button',{name:'生成聚合草稿',exact:true})).toBeEnabled();
      for (let i=0;i<3;i++) await page.getByRole('checkbox',{name:names[i],exact:true}).setChecked(Boolean(bits & (1<<i)));
      const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
      await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
      const {run_id} = await acceptedRun(await accepted);
      await expect.poll(async () => (await other.get('/api/run-evidence?run_id='+run_id)).body.memory?.aggregation?.graph_result).toBe('Completed');
      const facts = (await other.get('/api/run-evidence?run_id='+run_id)).body.memory.aggregation;
      expect(facts.provided).toHaveLength(names.filter((_,i)=>bits & (1<<i)).length);
      expect(facts.steps).toHaveLength(1);
    }
    await page.screenshot({path:'tmp/agent-work/issue-27/aggregation-combinations.png',fullPage:true});
  } finally { await server.close(); }
});

for (const surface of ['mainbar','behaviour','runs']) for (const timing of ['opened','late']) {
  test(`STD-01/SPEC-01: ${surface} clears offline sources and ${timing} reads on delete`, async ({page}) => {
    const server = await memoryServer({script:'tests/browser/aggregation_server.py'});
    let release = () => {};
    try {
      const {other,memoryId} = await prepareDraft(page,server);
      const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
      await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
      const {run_id} = await acceptedRun(await accepted);
      await expect(page.getByRole('button',{name:'生成聚合草稿',exact:true})).toBeEnabled();
      if (surface === 'runs') await page.goto(server.origin+'/runs/'+run_id);
      const area = surface === 'mainbar' ? page.locator('#messages') : surface === 'behaviour' ? page.getByRole('region',{name:'手动聚合',exact:true}) : page.getByRole('region',{name:'运行过程',exact:true});
      if (surface !== 'mainbar') {
        const collapse=page.getByRole('button',{name:'收起对话',exact:true});
        if (await collapse.isVisible()) await collapse.click();
      }
      const source = area.getByRole('button',{name:'语义记忆 S1',exact:true});
      await source.click();
      await expect(area.getByText('coffee source',{exact:true})).toBeVisible();
      await page.evaluate(() => window.dispatchEvent(new Event('offline')));
      await expect(area.getByText('coffee source',{exact:true})).toHaveCount(0);
      await page.evaluate(() => window.dispatchEvent(new Event('online')));
      await expect(page.locator('#connection')).not.toContainText('离线');
      await source.click();
      await expect(area.getByText('coffee source',{exact:true})).toBeVisible();
      let captured;
      const started = new Promise(resolve => { captured=resolve; });
      const delayed = new Promise(resolve => { release=resolve; });
      if (timing === 'late') await page.route('**/api/memory/record?*',async route => {
        const response=await route.fetch(); captured(); await delayed;
        await route.fulfill({response});
      });
      let responseDone;
      if (timing === 'late') {
        responseDone = page.waitForResponse(r => r.url().includes('/api/memory/record?'));
        await source.click();
        await started;
      }
      const gone=await other.command({operation_id:'late-delete',kind:'semantic',action:'delete',expected_version:1,payload:{id:memoryId}});
      expect(gone.status).toBe(200);
      expect(gone.body.forgetting.state).toBe('complete');
      release();
      if (responseDone) {
        await (await responseDone).finished();
        // Browser task barrier after the entire old response is delivered.
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      }
      await expect(area.getByText('coffee source',{exact:true})).toHaveCount(0);
      await expect(area).toContainText(/不可用|已变化|无法核验/);
      await page.unroute('**/api/memory/record?*');
      await page.goto(server.origin+'/behaviour');
      const expand=page.getByRole('button',{name:'展开对话',exact:true});
      if(await expand.isVisible()) await expand.click();
      await expect(page.locator('#messages').getByText('已验证草稿 [[S1]]',{exact:true})).toBeVisible();
    } finally { release(); await server.close(); }
  });
}


test('SPEC-03 CE-08: accepted POST remains fixed while response waits and Session/form change', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/aggregation_server.py'});
  let release = () => {};
  try {
    const {other,session} = await prepareDraft(page,server);
    let acknowledge;
    const accepted = new Promise(resolve => { acknowledge=resolve; });
    const delayed = new Promise(resolve => { release=resolve; });
    const submitted=[];
    await page.route('**/api/runs',async route => {
      if(route.request().method() !== 'POST') return route.continue();
      submitted.push(route.request().postDataJSON());
      const response=await route.fetch();
      acknowledge(await acceptedRun(response, route.request().postDataJSON()));
      await delayed;
      await route.fulfill({response});
    });
    await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
    const {run_id} = await accepted;
    await expect.poll(async () => (await other.get('/api/run-evidence?run_id='+run_id)).body.memory?.aggregation?.graph_result).toBe('Completed');
    // The real server has accepted/completed A, but its HTTP acceptance has
    // not reached the form. All following changes occur with that POST pending.
    await page.getByRole('button',{name:'新建会话',exact:true}).click();
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem('alfred.session'))).not.toBe(session);
    const second=await page.evaluate(() => sessionStorage.getItem('alfred.session'));
    await page.getByRole('button',{name:'刷新会话',exact:true}).click();
    await page.getByRole('combobox',{name:'目标会话'}).selectOption(second);
    await page.getByRole('textbox',{name:'聚合目标',exact:true}).fill('CHANGED_GOAL');
    await page.getByRole('textbox',{name:'聚合关键词',exact:true}).fill('CHANGED_KEYWORDS');
    await page.getByRole('checkbox',{name:'语义记忆',exact:true}).uncheck();
    const delivered=page.waitForResponse(r=>r.url().endsWith('/api/runs') && r.request().method()==='POST');
    release(); await (await delivered).finished();
    await expect(page.getByText('本次聚合已结束；草稿请在目标会话查看。')).toBeVisible();
    const facts=(await other.get('/api/run-evidence?run_id='+run_id)).body.memory.aggregation;
    expect(facts.request).toEqual({session_id:session,goal:'draft goal',keywords:'coffee',sources:['semantic']});
    expect(submitted).toEqual([{purpose:'aggregation',session_id:session,message:'draft goal',keywords:'coffee',sources:['semantic']}]);
    expect((await other.get('/api/runs?filter=chat&limit=25')).body.runs.filter(r=>r.purpose==='aggregation')).toHaveLength(1);
    await expect(page.locator('#messages')).not.toContainText('已验证草稿');
    await expect(page.getByRole('textbox',{name:'聚合目标',exact:true})).toHaveValue('CHANGED_GOAL');
  } finally { release(); await server.close(); }
});

test('SPEC-03 CE-11: partial stream transport failure preserves aborted Attempt without a draft', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/aggregation_server.py'});
  try {
    const {other} = await prepareDraft(page,server);
    await server.send('fail-model'); await server.send('hold-model');
    const accepted=page.waitForResponse(r=>r.url().endsWith('/api/runs') && r.request().method()==='POST');
    await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
    const {run_id}=await acceptedRun(await accepted);
    await server.send('wait-stream');
    await expect(page.locator('#messages')).not.toContainText('不得展示的候选流');
    await server.send('release-model');
    await expect.poll(async () => (await other.get('/api/runs?filter=chat&limit=25')).body.runs.find(r=>r.run_id===run_id)?.outcome).toBe('failed');
    const evidence=(await other.get('/api/run-evidence?run_id='+run_id)).body;
    expect(evidence.memory.aggregation.reply_disposition).toBe('no_reply');
    expect(evidence.memory.aggregation.steps).toHaveLength(1);
    expect(evidence.memory.aggregation.fallback.decision).toBe('blocked');
    // Read actual persisted AttemptAborted evidence from the real trace bundle.
    const {readFile,readdir}=await import('node:fs/promises');
    const {join}=await import('node:path');
    const files=await readdir(join(server.directory,'traces'),{recursive:true});
    let trace='';
    for(const file of files.filter(f=>f.endsWith('.jsonl'))) trace+=await readFile(join(server.directory,'traces',file),'utf8');
    expect(trace).toContain('attempt.aborted');
    expect(trace).toContain('stream_disconnected');
    expect(trace).toContain('不得展示的候选流');
    await page.reload();
    await expect(page.locator('#messages')).not.toContainText('不得展示的候选流');
    await expect(page.locator('#messages')).not.toContainText('已验证草稿');
    await expect(page.locator('#messages').getByRole('heading',{name:'聚合草稿',exact:true})).toHaveCount(0);
  } finally { await server.close(); }
});

for (const rounds of [0, 1, 3]) {
  test(`SPEC-07 rounds=${rounds}: real history exclusion in Behaviour, MainBar and Run`, async ({page}) => {
    const {spawn} = await import('node:child_process');
    const server = await memoryServer({script:'tests/browser/routing_server.py', spawnProcess: (command, args) => spawn(command, [...args, '--working-memory-rounds', String(rounds)])});
    try {
      await page.goto(server.origin+'/behaviour');
      await page.getByRole('button',{name:'新建会话',exact:true}).click();
      await page.getByRole('button',{name:'展开对话',exact:true}).click();
      const other = await api(page.request,server.origin);
      const history = [];
      for (const text of ['old history question','new history question']) {
        await page.getByRole('textbox',{name:'消息',exact:true}).fill(text);
        const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method()==='POST');
        await page.getByRole('button',{name:'发送',exact:true}).click();
        const response = await accepted;
        expect(response.status()).toBe(202);
        const {run_id} = await response.json();
        history.push(run_id);
        await expect.poll(async () => (await other.get('/api/runs?filter=chat&limit=25')).body.runs.find(r => r.run_id===run_id)?.outcome).toBe('completed');
      }
      await page.getByRole('button',{name:'刷新会话',exact:true}).click();
      const session = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
      const form = page.getByRole('region',{name:'手动聚合',exact:true});
      await form.getByRole('combobox',{name:'目标会话'}).selectOption(session);
      await form.getByRole('textbox',{name:'聚合目标',exact:true}).fill('history goal');
      for (const name of ['语义记忆','情景记忆']) await form.getByRole('checkbox',{name,exact:true}).uncheck();
      const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method()==='POST');
      await form.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
      const response = await accepted;
      expect(response.status()).toBe(202);
      const {run_id} = await response.json();
      await expect.poll(async () => (await other.get('/api/run-evidence?run_id='+run_id)).body.memory?.aggregation?.graph_result).toBe(rounds ? 'Completed' : 'NoAction');
      const evidence = await other.get('/api/run-evidence?run_id='+run_id);
      expect(evidence.status).toBe(200);
      const facts = evidence.body.memory.aggregation;
      const provided = Math.min(2,rounds);
      expect(facts.sources.history).toMatchObject({candidate_count:2,approved_count:2,round_excluded:2-provided,capacity_excluded:0,request_excluded:0,actual_input_count:provided});
      expect((facts.provided || []).map(r => r.run_id)).toEqual(provided ? history.slice(-provided) : []);
      const text = `会话窗口：已读取；实际提供 ${provided}；容量排除 0；轮数排除 ${2-provided}；许可排除 0；未取完状态 none`;
      await expect(form.getByText(text,{exact:true})).toBeVisible();
      await expect(page.locator('#messages').getByText(text,{exact:true})).toBeVisible();
      if (rounds === 0) {
        expect(facts.reason_code).toBe('capacity_excluded_all');
        expect(facts.steps).toHaveLength(0);
        expect(evidence.body.memory.input_attempts).toHaveLength(0);
        expect(evidence.body.attempts).toHaveLength(0);
        await expect(form.getByText('容量导致无可用资料',{exact:true})).toBeVisible();
        await expect(page.locator('#messages').getByRole('heading',{name:'聚合草稿',exact:true})).toHaveCount(0);
        await expect(page.locator('#messages').getByText('离线路由回复',{exact:true})).toHaveCount(2);
      } else {
        expect(facts.steps).toHaveLength(1);
        await expect(page.locator('#messages').getByRole('heading',{name:'聚合草稿',exact:true})).toBeVisible();
      }
      await page.reload();
      await expect(page.locator('#messages').getByText(text,{exact:true})).toBeVisible();
      await page.goto(server.origin+'/runs/'+run_id);
      const area = page.getByRole('region',{name:'运行过程',exact:true});
      await expect(area.getByText(text,{exact:true})).toBeVisible();
      if (rounds === 0) await expect(area.getByText('容量导致无可用资料',{exact:true})).toBeVisible();
    } finally {await server.close();}
  });
}


async function deliverSessionList(page, release) {
  const delivered = page.waitForResponse(r => r.url().includes('/api/sessions?') && r.request().method() === 'GET');
  release();
  await (await delivered).finished();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

test('CI-01: delayed initial Session list cannot erase a newer selected target', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/aggregation_server.py'});
  let release = () => {};
  try {
    let first = true, captured;
    const received = new Promise(resolve => { captured = resolve; });
    const held = new Promise(resolve => { release = resolve; });
    await page.route('**/api/sessions?*', async route => {
      if (!first) return route.continue();
      first = false;
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      expect((await response.json()).sessions).toEqual([]);
      captured(); await held; await route.fulfill({response});
    });
    const {other, session} = await prepareDraft(page, server, () => received);
    await expect(page.getByRole('combobox',{name:'目标会话'})).toHaveValue(session);
    await deliverSessionList(page, release);
    // Keep the original HTTP acceptance assertion as well as the selected value.
    const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
    await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
    const response = await accepted, body = await response.json();
    expect(response.status(), JSON.stringify({body,payload:response.request().postDataJSON()})).toBe(202);
    expect(response.request().postDataJSON().session_id).toBe(session);
    await expect(page.getByRole('combobox',{name:'目标会话'})).toHaveValue(session);
    await expect.poll(async () => (await other.get('/api/run-evidence?run_id='+body.run_id)).body.memory?.aggregation?.graph_result).toBe('Completed');
    expect((await other.get('/api/runs?filter=chat&limit=25')).body.runs.filter(r => r.purpose === 'aggregation')).toHaveLength(1);
  } finally { release(); await server.close(); }
});

test('CI-01: an in-flight Session refresh preserves the latest user selection', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/aggregation_server.py'});
  let release = () => {};
  try {
    const {other, session} = await prepareDraft(page, server);
    const created = page.waitForResponse(r => r.url().endsWith('/api/sessions') && r.request().method() === 'POST');
    await page.getByRole('button',{name:'新建会话',exact:true}).click();
    const response = await created;
    expect(response.status()).toBe(201);
    const second = (await response.json()).session_id;
    const refreshed = page.waitForResponse(r => r.url().includes('/api/sessions?'));
    await page.getByRole('button',{name:'刷新会话',exact:true}).click();
    await (await refreshed).finished();
    const target = page.getByRole('combobox',{name:'目标会话'});
    await target.selectOption(session);
    let captured;
    const received = new Promise(resolve => { captured = resolve; });
    const held = new Promise(resolve => { release = resolve; });
    await page.route('**/api/sessions?*', async route => {
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      expect((await response.json()).sessions.map(s => s.session_id)).toEqual(expect.arrayContaining([session,second]));
      captured(); await held; await route.fulfill({response});
    });
    await page.getByRole('button',{name:'刷新会话',exact:true}).click();
    await received;
    await target.selectOption(second);
    await deliverSessionList(page, release);
    await expect(target).toHaveValue(second);
    const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
    await page.getByRole('button',{name:'生成聚合草稿',exact:true}).click();
    const result = await accepted, body = await result.json();
    expect(result.status(), JSON.stringify({body,payload:result.request().postDataJSON()})).toBe(202);
    expect(body.session_id).toBe(second);
    await expect.poll(async () => (await other.get('/api/run-evidence?run_id='+body.run_id)).body.memory?.aggregation?.request?.session_id).toBe(second);
  } finally { release(); await server.close(); }
});
