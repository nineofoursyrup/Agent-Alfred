import {test, expect} from '@playwright/test';

test('one shell retains SQL and consumes one mutually exclusive panel history layer', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  await page.goto('/database');
  const sql = page.getByRole('textbox',{name:'SQL',exact:true});
  await sql.fill('SELECT 17 AS retained_draft');
  await page.getByRole('button',{name:'打开导航',exact:true}).click();
  await expect(sql).not.toBeInViewport();
  await page.getByRole('button',{name:'打开主对话',exact:true}).click();
  await expect(page.locator('#mainbar')).toBeVisible();
  await page.goBack();
  await expect(sql).toBeVisible();
  await expect(sql).toHaveValue('SELECT 17 AS retained_draft');
  await page.goForward();
  await expect(page.locator('#mainbar')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(sql).toBeVisible();
  await page.getByRole('button',{name:'打开导航',exact:true}).click();
  await page.getByRole('link',{name:'收件箱',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'离开当前页面？'})).toBeVisible();
  await page.getByRole('button',{name:'留在此页',exact:true}).click();
  await expect(page).toHaveURL(/\/database$/);
  await page.getByRole('link',{name:'收件箱',exact:true}).click();
  await page.getByRole('button',{name:'放弃并离开',exact:true}).click();
  await expect(page).toHaveURL(/\/inbox$/);
  await page.goBack();
  await expect(page).toHaveURL(/\/database$/);
  await expect(sql).toBeVisible();
  await expect(sql).toHaveValue('');
});

test('browser history cancellation preserves the actual page, draft, focus and repeatable Back', async ({page}) => {
  await page.goto('/inbox');
  await page.locator('nav a[href="/database"]').click();
  const sql = page.getByRole('textbox',{name:'SQL',exact:true});
  await sql.fill('SELECT 18');
  await page.goBack();
  await expect(page.getByRole('button',{name:'留在此页',exact:true})).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page).toHaveURL(/\/database$/);
  await expect(sql).toHaveValue('SELECT 18');
  await expect(sql).toBeFocused();
  await page.goBack();
  await page.getByRole('button',{name:'放弃并离开',exact:true}).click();
  await expect(page).toHaveURL(/\/inbox$/);
  await page.goForward();
  await expect(sql).toHaveValue('');
});

test('wide shell uses fixed rails, keeps MainBar and does not remount the same route', async ({page}) => {
  await page.setViewportSize({width:1440,height:900});
  await page.goto('/database');
  await expect(page.locator('#mainbar')).toBeVisible();
  expect((await page.locator('.site-header').boundingBox()).width).toBe(232);
  expect((await page.locator('#mainbar').boundingBox()).width).toBe(320);
  const sql=page.getByRole('textbox',{name:'SQL',exact:true});
  await sql.fill('SELECT 19');
  await page.locator('nav a[href="/database"]').click();
  await expect(sql).toHaveValue('SELECT 19');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button',{name:'收起主对话',exact:true}).click();
  await expect(page.locator('#mainbar')).toBeHidden();
  await page.getByRole('button',{name:'打开主对话',exact:true}).click();
  await expect(sql).toHaveValue('SELECT 19');
});

test('refresh retires the panel entry without leaving a same-page Back stop',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await page.goto('/inbox');
  await page.getByRole('button',{name:'打开导航',exact:true}).click();
  await page.locator('nav a[href="/database"]').click();
  await expect(page).toHaveURL(/\/database$/);
  await page.getByRole('button',{name:'打开主对话',exact:true}).click();
  await page.reload();
  await expect(page.getByRole('textbox',{name:'SQL',exact:true})).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/inbox$/);
});

test('all nine existing pages keep one native EventSource and the selected Session draft',async({page})=>{
  await page.addInitScript(()=>{
    const Native=window.EventSource;window.observedSources=[];
    window.EventSource=class extends Native{constructor(...args){super(...args);window.observedSources.push(this);}close(){this.observedClosed=true;super.close();}};
  });
  const posts=[];page.on('request',request=>{if(request.method()==='POST')posts.push(request.url());});
  await page.goto('/inbox');
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  await page.getByRole('textbox',{name:'消息'}).fill('九页共享且不发送的草稿');
  const identity=await page.evaluate(()=>sessionStorage.getItem('alfred.session'));
  const connections=await page.evaluate(()=>window.observedSources.length);
  for(const path of ['runs','memory','models','connections','behaviour','tools','ops','database','inbox']){
    await page.locator(`nav a[href="/${path}"]`).click();
    await expect(page.locator(`nav a[href="/${path}"]`)).toHaveAttribute('aria-current','page');
    await expect(page.getByRole('textbox',{name:'消息'})).toHaveValue('九页共享且不发送的草稿');
    expect(await page.evaluate(()=>sessionStorage.getItem('alfred.session'))).toBe(identity);
    expect(await page.evaluate(()=>window.observedSources.length)).toBe(connections);
    expect(await page.evaluate(()=>window.observedSources.filter(source=>!source.observedClosed).length)).toBe(1);
  }
  expect(posts.filter(url=>url.endsWith('/api/sessions'))).toHaveLength(1);
  expect(posts.filter(url=>url.endsWith('/api/runs'))).toHaveLength(0);
});

test('late accepted response preserves new chat input and the users central focus',async({page})=>{
  await page.goto('/inbox');
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  const input=page.getByRole('textbox',{name:'消息'});
  await input.fill('本次原始请求');
  await expect(page.getByRole('button',{name:'发送',exact:true})).toBeEnabled();
  let release,captured;const gate=new Promise(resolve=>release=resolve);const held=new Promise(resolve=>captured=resolve);
  await page.route('**/api/runs',async route=>{const response=await route.fetch();captured();await gate;await route.fulfill({response});});
  try{
    await input.press('Enter');await held;
    await input.fill('202 之后也不能清掉的新输入');
    await page.locator('nav a[href="/database"]').click();
    const sql=page.getByRole('textbox',{name:'SQL',exact:true});await sql.fill('SELECT 27');
    release();
    await expect(page.getByRole('region',{name:'主对话'})).toContainText('本次原始请求');
    await expect(input).toHaveValue('202 之后也不能清掉的新输入');
    await expect(sql).toBeFocused();
  }finally{release();}
});

test('storage denial keeps the in-memory draft and reports its limited lifetime',async({page})=>{
  await page.addInitScript(()=>{Object.defineProperty(window,'sessionStorage',{get(){throw new DOMException('denied','SecurityError');}});});
  await page.goto('/inbox');
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  await page.getByRole('textbox',{name:'消息'}).fill('仅本页仍可编辑');
  await page.locator('nav a[href="/runs"]').click();
  await expect(page.getByRole('textbox',{name:'消息'})).toHaveValue('仅本页仍可编辑');
  await expect(page.locator('#storage-warning')).toContainText('刷新可能无法恢复');
});

test('crossing the breakpoint keeps editing selection without overwriting the wide preference',async({page})=>{
  await page.setViewportSize({width:1100,height:800});
  await page.goto('/inbox');await page.getByRole('button',{name:'新建会话',exact:true}).click();
  const input=page.getByRole('textbox',{name:'消息'});await input.fill('保留选区和当前输入');
  await input.evaluate(input=>input.setSelectionRange(2,5));
  await page.setViewportSize({width:1099,height:800});
  await expect(input).toBeVisible();await expect(input).toBeFocused();
  expect(await input.evaluate(input=>[input.selectionStart,input.selectionEnd])).toEqual([2,5]);
  await page.setViewportSize({width:1100,height:800});
  await expect(input).toBeFocused();
  await page.getByRole('button',{name:'收起主对话',exact:true}).click();
  await page.locator('nav a[href="/database"]').click();
  const sql=page.getByRole('textbox',{name:'SQL',exact:true});await sql.fill('SELECT 29');
  await page.setViewportSize({width:1099,height:800});await expect(sql).toBeFocused();
  await page.setViewportSize({width:1100,height:800});await expect(sql).toBeFocused();
  await expect(page.locator('#mainbar')).toBeHidden();
});

test('Forward cancellation after reload restores the correct old-document history direction',async({page})=>{
  await page.goto('/inbox');
  await page.locator('nav a[href="/database"]').click();
  await page.locator('nav a[href="/runs"]').click();
  await page.goBack();await page.reload();
  const sql=page.getByRole('textbox',{name:'SQL',exact:true});await sql.fill('SELECT 43');
  await page.goForward();await page.getByRole('button',{name:'留在此页',exact:true}).click();
  await expect(page).toHaveURL(/\/database$/);
  await expect(sql).toHaveValue('SELECT 43');await expect(sql).toBeFocused();
  await page.goForward();await page.getByRole('button',{name:'放弃并离开',exact:true}).click();
  await expect(page).toHaveURL(/\/runs$/);
});

test('Back restores the central scroll only after the list response returns',async({page})=>{
  await page.goto('/inbox');
  for(let index=0;index<35;index++){
    const response=await page.request.get('/api/entry');const entry=await response.json();
    await page.request.post('/api/sessions',{headers:{'x-agent-alfred-csrf':entry.csrf_token},data:{}});
  }
  await page.reload();await expect(page.locator('#page .card').first()).toBeVisible();
  await page.locator('#page').evaluate(element=>element.scrollTop=400);
  const top=await page.locator('#page').evaluate(element=>element.scrollTop);expect(top).toBeGreaterThan(100);
  await page.locator('nav a[href="/models"]').click();
  let release,entered;const gate=new Promise(r=>release=r),captured=new Promise(r=>entered=r);
  await page.route('**/api/sessions?*',async route=>{const response=await route.fetch();entered();await gate;await route.fulfill({response});});
  await page.goBack();await captured;release();
  await expect.poll(()=>page.locator('#page').evaluate(element=>element.scrollTop)).toBe(top);
  await expect(page.getByRole('heading',{name:'收件箱',exact:true})).toBeFocused();
});

for(const target of ['memory','database'])test(`hidden ${target} still withdraws protected content through the real stream`,async({page})=>{
  const {api,memoryServer}=await import('./memory-server.js');const server=await memoryServer();
  try{
    const other=await api(page.request,server.origin);
    const saved=await other.command({operation_id:'shell-hidden-seed',kind:'semantic',action:'save',payload:{subject:'hidden',fact:'hidden-sensitive-body'}});
    await page.setViewportSize({width:390,height:844});await page.goto(server.origin+'/'+target);
    const content=target==='database'?page.getByRole('region',{name:'查询结果',includeHidden:true}):page.getByLabel('语义记忆列表');
    if(target==='database'){
      await page.getByRole('textbox',{name:'SQL',exact:true}).fill('SELECT fact FROM diag_facts');
      await page.getByRole('button',{name:'执行',exact:true}).click();
    }
    await expect(content).toContainText('hidden-sensitive-body');
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    await expect(page.locator('#page')).toHaveAttribute('aria-hidden','true');
    const deleted=await other.command({operation_id:'shell-hidden-delete',kind:'semantic',action:'delete',expected_version:1,payload:{id:saved.body.result.memory_id}});
    expect(deleted.body.result.status).toBe('deleted');
    await expect(content).not.toContainText('hidden-sensitive-body');
    await page.keyboard.press('Escape');await expect(content).not.toContainText('hidden-sensitive-body');
    if(target==='database')await expect(page.getByRole('textbox',{name:'SQL',exact:true})).toHaveValue('SELECT fact FROM diag_facts');
  }finally{await server.close();}
});
