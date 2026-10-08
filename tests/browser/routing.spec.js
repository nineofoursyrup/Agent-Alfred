import {observeTopology} from './topology-observation.js';
import {test, expect} from '@playwright/test';
import {memoryServer, api} from './memory-server.js';

test('S06 CE-13: read-only comparison cannot rebase a dirty routing choice', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin + '/behaviour');
    const choice = page.getByRole('checkbox', {name:'启用消息分流'});
    await expect(choice).toBeEnabled();
    const saved = await (await page.request.get(server.origin + '/api/behaviour')).json();
    await choice.check();
    const {csrf_token} = await (await page.request.get(server.origin + '/api/entry')).json();
    const external = await page.request.post(server.origin + '/api/behaviour', {
      headers:{'x-agent-alfred-csrf':csrf_token},
      data:{action:'save',enabled:true,expected_revision:saved.revision},
    });
    expect(external.status()).toBe(200);
    const refreshed = page.waitForResponse(r => r.url().endsWith('/api/behaviour') && r.request().method()==='GET');
    await page.getByRole('button',{name:'刷新设置',exact:true}).click();
    await refreshed;
    await observeTopology(page);
    await choice.uncheck(); // Still an edit of the original version, not consent to the external revision.
    await choice.check();
    const attempted = page.waitForResponse(r => r.url().endsWith('/api/behaviour') && r.request().method()==='POST');
    await page.getByRole('button',{name:'保存设置',exact:true}).click();
    const response = await attempted;
    expect(response.request().postDataJSON().expected_revision).toBe(saved.revision);
    expect(response.status()).toBe(409);
    await expect(page.getByText(/settings_conflict/)).toBeVisible();
    await expect(choice).toBeChecked();
    await page.getByRole('button',{name:'基于当前版本继续编辑',exact:true}).click();
    await choice.uncheck();
    await page.getByRole('button',{name:'保存设置',exact:true}).click();
    await expect(page.getByText('已保存；下一 Run 生效。',{exact:true})).toBeVisible();
    expect((await (await page.request.get(server.origin+'/api/behaviour')).json()).enabled).toBe(false);
  } finally {await server.close();}
});

test('CE-07/09/12: Behaviour, no reply, refresh and real restart', async ({page}) => {
  const server = await memoryServer({script: 'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin + '/behaviour');
    await page.getByRole('checkbox', {name: '启用消息分流'}).check();
    await page.getByRole('button', {name: '保存设置', exact: true}).click();
    await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
    await page.getByRole('button', {name: '新建会话', exact: true}).click();
    await expect(page.getByRole('textbox', {name: '消息'})).toBeEnabled();
    const session = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    await page.getByRole('textbox', {name: '消息'}).fill('不用回复');
    const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
    await page.getByRole('button', {name: '发送', exact: true}).click();
    const admission = await accepted, identity = await admission.json();
    expect(admission.status(), JSON.stringify(identity)).toBe(202);
    expect(identity).toMatchObject({run_id: expect.any(String), session_id: session});
    const messages = page.locator('#messages');
    await expect(messages.getByText('已结束 · 按要求未回复')).toBeVisible();
    await expect(messages.getByText('运行已结束', {exact:true})).toHaveCount(0);
    // A no-reply terminal projection is visible before recording commits.
    await expect(messages.getByText('已保存', {exact:true})).toBeVisible();
    const other = await api(page.request, server.origin);
    const runs = await other.get('/api/runs?filter=chat&limit=25');
    expect(runs.status, JSON.stringify(runs.body)).toBe(200);
    const recorded = runs.body.runs.find(r => r.run_id === identity.run_id);
    expect(recorded).toMatchObject({session_id: session, prompt_preview: '不用回复', phase: 'finished', outcome: 'completed'});
    const run = recorded.run_id;
    await page.goto(`${server.origin}/runs/${run}`);
    await expect(page.getByText('user_requested_no_reply', {exact:false}).first()).toBeVisible();
    await server.restart();
    await page.reload();
    await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toBeVisible();
    const entry = await (await page.request.get(server.origin + '/api/entry')).json();
    const response = await page.request.get(server.origin + '/api/reply?' + new URLSearchParams({
      process_instance_id:entry.instance_id, session_id:session, run_id:run,
    }));
    expect(response.status()).toBe(200);
    expect((await response.json()).reply_disposition).toBe('no_reply');
    await page.goto(server.origin + '/behaviour');
    await expect(page.getByRole('checkbox', {name:'启用消息分流'})).toBeChecked();
  } finally {await server.close();}
});

test('CE-09/10: failed recording keeps no-reply projection and rejects next Run', async ({page}) => {
  const server = await memoryServer({script: 'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin + '/behaviour');
    await page.getByRole('checkbox', {name:'启用消息分流'}).check();
    await page.getByRole('button', {name:'保存设置', exact:true}).click();
    await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
    await page.getByRole('button', {name:'新建会话', exact:true}).click();
    await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    await server.send('fail-recording');
    await page.getByRole('textbox', {name:'消息'}).fill('不用回复');
    await page.getByRole('button', {name:'发送', exact:true}).click();
    await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toBeVisible();
    await expect(page.locator('#messages').getByText('本次运行未保存', {exact:true})).toBeVisible();
    await page.reload();
    await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toBeVisible();
    await expect(page.locator('#messages').getByText('正文未完整加载')).toHaveCount(0);
    await expect(page.getByRole('button', {name:'发送', exact:true})).toBeDisabled();
    const href = await page.getByRole('region', {name:'当前运行', exact:true}).getByRole('link', {name:'查看当前运行'}).getAttribute('href');
    const run = {run_id:new URL(href, server.origin).pathname.split('/').pop(),
      session_id:await page.evaluate(() => sessionStorage.getItem('alfred.session'))};
    await server.send('repair-recording');
    await server.restart();
    await page.goto(`${server.origin}/runs/${run.run_id}`);
    await expect(page.getByText(/运行终态无法确认|执行前中断/).first()).toBeVisible();
    const entry = await (await page.request.get(server.origin + '/api/entry')).json();
    const response = await page.request.get(server.origin + '/api/reply?' + new URLSearchParams({
      process_instance_id:entry.instance_id, session_id:run.session_id, run_id:run.run_id,
    }));
    expect(response.status()).toBe(503);
    expect((await response.json()).code).toBe('reply_unavailable');
    await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toHaveCount(0);
  } finally {await server.close();}
});

test('CE-09: recovery facts and fixed text survive trace removal and restart', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin + '/behaviour');
    await page.getByRole('checkbox', {name:'启用消息分流'}).check();
    await page.getByRole('button', {name:'保存设置', exact:true}).click();
    await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
    await server.send('corrupt-context');
    await page.getByRole('button', {name:'新建会话', exact:true}).click();
    await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    for (const task of ['不用回复', '解释上下文']) {
      await page.getByRole('textbox', {name:'消息'}).fill(task);
      await page.getByRole('button', {name:'发送', exact:true}).click();
      if (task === '不用回复')
        await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toBeVisible();
      else
        await expect(page.locator('#messages').getByText('本次上下文准备失败，请稍后重试。')).toBeVisible();
      await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
    }
    await server.send('trim-traces');
    await server.restart();
    await page.reload();
    await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toBeVisible();
    await expect(page.locator('#messages').getByText('本次上下文准备失败，请稍后重试。')).toBeVisible();
    const other = await api(page.request, server.origin);
    const runs = await other.get('/api/runs?filter=chat&limit=25');
    for (const run of runs.body.runs) {
      await page.goto(`${server.origin}/runs/${run.run_id}`);
      await expect(page.getByText('project_context', {exact:false}).first()).toBeVisible();
    }
  } finally {await server.close();}
});

test('CE-13: page fingerprint rejects external change and explicitly backs up recovery', async ({page}) => {
  const {writeFile, readFile, readdir} = await import('node:fs/promises');
  const {join} = await import('node:path');
  const server = await memoryServer({script:'tests/browser/routing_server.py',
    prepare: directory => writeFile(join(directory, 'behaviour.json'), 'broken A')});
  try {
    await page.goto(server.origin + '/behaviour');
    const recover = page.getByRole('button', {name:'备份原文件并恢复为关闭'});
    await expect(recover).toBeVisible();
    await writeFile(join(server.directory, 'behaviour.json'), 'broken B');
    await recover.click();
    await expect(page.getByText(/settings_conflict/)).toBeVisible();
    await observeTopology(page); // #75 CE-01: graph reads preserve the real conflict.
    await expect(page.getByText(/settings_conflict/)).toBeVisible();
    expect(await readFile(join(server.directory, 'behaviour.json'), 'utf8')).toBe('broken B');
    await page.getByRole('button', {name:'刷新设置'}).click();
    await expect(page.getByText(/配置不可用/)).toBeVisible();
    await recover.click();
    await expect(page.getByText('已备份原文件并恢复为关闭；下一 Run 生效。')).toBeVisible();
    const backup = (await readdir(server.directory)).find(p => p.startsWith('behaviour.json.backup-'));
    expect(await readFile(join(server.directory, backup), 'utf8')).toBe('broken B');
    await server.restart();
    await page.reload();
    await expect(page.getByRole('checkbox', {name:'启用消息分流'})).not.toBeChecked();
  } finally {await server.close();}
});

test('CE-01: disabled real browser conversation matches exact pre-routing baseline', async ({browser}) => {
  const {spawn, execFileSync} = await import('node:child_process');
  const {mkdtemp, rm} = await import('node:fs/promises');
  const {tmpdir} = await import('node:os');
  const {join, resolve} = await import('node:path');
  const baseline = await mkdtemp(join(tmpdir(), 'routing-baseline-'));
  const root = process.cwd();
  const archive = execFileSync('git', ['archive', '8ba7192ee84533aef3deb8453d8fa7364d67f48b'], {maxBuffer:64 * 1024 * 1024});
  execFileSync(resolve('.venv/bin/python'), ['-c',
    'import io,sys,tarfile; tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())).extractall(sys.argv[1],filter="data")', baseline], {input:archive});
  const texts = [];
  try {
    for (const source of [baseline, root]) {
      const server = await memoryServer({script:'tests/browser/skills_server.py',
        spawnProcess: (_command, args) => spawn(resolve(root, '.venv/bin/python'), args, {cwd:source})});
      const context = await browser.newContext();
      try {
        const page = await context.newPage();
        // Both versions open this real stream only after consuming entry credentials.
        const ready = page.waitForRequest(request => request.url() === server.origin + '/api/events');
        await page.goto(server.origin + '/inbox');
        await ready;
        await page.getByRole('button', {name:'新建会话', exact:true}).click();
        await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
        if(source===baseline)await page.getByRole('button',{name:'展开对话',exact:true}).click();
        else await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
        await page.getByRole('textbox', {name:'消息'}).fill('/skills off\nhello');
        await page.getByRole('button', {name:'发送', exact:true}).click();
        await expect(page.locator('#messages').getByText('离线 Skill 回复')).toBeVisible();
        await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
        await page.reload();
        await expect(page.locator('#messages').getByText('离线 Skill 回复')).toBeVisible();
        texts.push(await page.locator('#messages').innerText());
      } finally {
        try {await context.close();} finally {await server.close();}
      }
    }
    expect(texts[1]).toBe(texts[0]);
  } finally {await rm(baseline, {recursive:true, force:true});}
});

test('S06 CE-13: a delayed save receipt preserves the choice edited after submission',async({page})=>{
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  let release = () => {};
  try {
    await page.goto(server.origin+'/behaviour');
    const choice = page.getByRole('checkbox',{name:'启用消息分流'});
    await choice.check();
    let accepted;
    const started = new Promise(resolve=>accepted=resolve), gate = new Promise(resolve=>release=resolve);
    await page.route('**/api/behaviour',async route=>{
      if(route.request().method()!=='POST')return route.continue();
      const response = await route.fetch();accepted();await gate;await route.fulfill({response});
    });
    await page.getByRole('button',{name:'保存设置',exact:true}).click();await started;
    await choice.uncheck();
    await expect(page.getByRole('button',{name:'刷新设置',exact:true})).toBeDisabled();
    release();
    await expect(page.getByText('已保存；下一 Run 生效。',{exact:true})).toBeVisible();
    await expect(choice).not.toBeChecked();
    await expect(page.getByText(/未提交选择：关闭 · 编辑基线 revision 1/)).toBeVisible();
    await page.getByRole('button',{name:'刷新设置',exact:true}).click();
    await expect(page.getByText(/已保存的分流设置：开启 · revision 1/)).toBeVisible();
    await expect(choice).not.toBeChecked();
    expect((await(await page.request.get(server.origin+'/api/behaviour')).json()).enabled).toBe(true);
  } finally {release();await server.close();}
});
