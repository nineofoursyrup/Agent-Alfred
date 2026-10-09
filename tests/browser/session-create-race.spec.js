import {test,expect} from '@playwright/test';
import {memoryServer} from './memory-server.js';

async function observeStream(page) {
  await page.addInitScript(()=>{
    const Native=window.EventSource;window.sessionActionStreams=[];
    window.EventSource=class extends Native {
      constructor(url){super(url);window.sessionActionStreams.push(this);}
    };
  });
}
async function selected(page) {
  return page.evaluate(()=>sessionStorage.getItem('alfred.session'));
}
async function runtime(page) {
  return page.evaluate(async()=>(await import('/assets/app.js')).dashboard.runtime());
}
async function setup(page,origin) {
  await observeStream(page);await page.goto(origin+'/inbox');
  const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/sessions'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  expect((await response).status()).toBe(201);
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toBeEnabled();
  await expect.poll(async()=>(await runtime(page)).connected).toBe(true);
  return selected(page);
}
// Hold only a genuine successful HTTP receipt. The creation, Session identity,
// Host state and later recording are always produced by the real service.
async function holdCreate(page) {
  let release,received,delivered;
  const gate=new Promise(resolve=>release=resolve),ready=new Promise(resolve=>received=resolve),done=new Promise(resolve=>delivered=resolve);
  let body;
  await page.route('**/api/sessions',async route=>{
    const response=await route.fetch();expect(response.status()).toBe(201);
    body=await response.json();received();await gate;
    await route.fulfill({response}).catch(()=>{});delivered();
  },{times:1});
  return {ready,get body(){return body;},async release(){release();await done;}};
}
async function oneStream(page) {
  expect(await page.evaluate(()=>window.sessionActionStreams.filter(source=>source.readyState!==EventSource.CLOSED).length)).toBe(1);
}

test('creating closes Send and Enter while a newer draft keeps the successful receipt explicit',async({page})=>{
  const server=await memoryServer({script:'tests/browser/overview_server.py'});let held;
  try {
    const a=await setup(page,server.origin);
    const input=page.getByRole('textbox',{name:'消息',exact:true});await input.fill('A 原草稿');
    const posts=[];page.on('request',request=>{if(request.method()==='POST')posts.push(new URL(request.url()).pathname);});
    held=await holdCreate(page);
    await page.getByRole('button',{name:'新建会话并打开主对话',exact:true}).click();await held.ready;
    await expect(page.getByRole('button',{name:'发送',exact:true})).toBeDisabled();
    await expect(page.locator('#send-reason')).toContainText('正在新建会话');
    await expect(input).toBeEnabled();await input.fill('创建等待期间编辑的 A 新草稿');await input.press('Enter');
    await page.evaluate(()=>new Promise(requestAnimationFrame));
    expect(posts).toEqual(['/api/sessions']);await expect(input).toHaveValue('创建等待期间编辑的 A 新草稿');
    const b=held.body.session_id;await held.release();
    const receipt=page.getByRole('region',{name:'新建会话回执',exact:true});
    await expect(receipt).toContainText(b);await expect(receipt).toContainText('已创建');
    expect(await selected(page)).toBe(a);await expect(input).toHaveValue('创建等待期间编辑的 A 新草稿');await expect(input).toBeFocused();
    await oneStream(page);expect(posts).toEqual(['/api/sessions']);
    await page.getByRole('button',{name:'收起主对话',exact:true}).click();await page.setViewportSize({width:320,height:900});
    await expect(receipt).toBeVisible();
    const bounds=await receipt.boundingBox();expect(bounds.x).toBeGreaterThanOrEqual(0);expect(bounds.x+bounds.width).toBeLessThanOrEqual(320);
    await receipt.getByRole('button',{name:'打开已创建会话',exact:true}).click();
    await expect.poll(()=>selected(page)).toBe(b);await expect(input).toHaveValue('');
    expect(await page.evaluate(id=>sessionStorage.getItem('alfred.draft:'+id),a)).toBe('创建等待期间编辑的 A 新草稿');
    await oneStream(page);expect(posts).toEqual(['/api/sessions']);
  } finally {if(held)await held.release();await server.close();}
});

test('an external real Run prevents late creation from switching the recording Session and never queues the switch',async({page,context})=>{
  const server=await memoryServer({script:'tests/browser/overview_server.py'});let held,other;
  try {
    const a=await setup(page,server.origin);const input=page.getByRole('textbox',{name:'消息',exact:true});await input.fill('A 在另一标签运行期间保留的草稿');
    other=await context.newPage();await other.goto(server.origin+'/inbox?'+new URLSearchParams({session_id:a,view:'messages'}));
    await other.getByRole('button',{name:'继续此会话',exact:true}).click();
    await other.getByRole('textbox',{name:'消息',exact:true}).fill('来自另一标签页的真实 Run');
    await expect(other.getByRole('button',{name:'发送',exact:true})).toBeEnabled();
    const posts=[];page.on('request',request=>{if(request.method()==='POST')posts.push(new URL(request.url()).pathname);});
    held=await holdCreate(page);await page.getByRole('button',{name:'新建会话并打开主对话',exact:true}).click();await held.ready;
    await server.send('hold-recording');
    const admitted=other.waitForResponse(r=>new URL(r.url()).pathname==='/api/runs'&&r.request().method()==='POST');
    await other.getByRole('button',{name:'发送',exact:true}).click();const response=await admitted;expect(response.status()).toBe(202);const {run_id}=await response.json();
    await server.send('wait-recording');
    await expect.poll(async()=>{const state=await runtime(page);return [state.session,state.active?.session_id,state.active?.run_id,state.active?.phase,state.active?.recording_state];}).toEqual([a,a,run_id,'finished','pending']);
    const streamCount=await page.evaluate(()=>window.sessionActionStreams.length);
    const b=held.body.session_id;await held.release();
    await expect.poll(()=>selected(page)).toBe(a);
    const receipt=page.getByRole('region',{name:'新建会话回执',exact:true});await expect(receipt).toContainText(b);await expect(receipt).toContainText('已创建');await expect(receipt).toContainText('尚未收尾');
    const open=receipt.getByRole('button',{name:'打开已创建会话',exact:true});await expect(open).toBeDisabled();
    await expect(input).toHaveValue('A 在另一标签运行期间保留的草稿');
    expect((await runtime(page)).active?.run_id).toBe(run_id);expect(await page.evaluate(()=>window.sessionActionStreams.length)).toBe(streamCount);await oneStream(page);
    const inspect=receipt.getByRole('link',{name:'查看已创建会话',exact:true});await inspect.focus();
    await server.send('release-recording');await expect.poll(async()=>(await runtime(page)).active).toBeNull();await expect(open).toBeEnabled();await expect(inspect).toBeFocused();
    await page.evaluate(()=>new Promise(requestAnimationFrame));expect(await selected(page)).toBe(a);expect(posts).toEqual(['/api/sessions']);
    await open.click();await expect.poll(()=>selected(page)).toBe(b);await expect(input).toHaveValue('');
    expect(await page.evaluate(id=>sessionStorage.getItem('alfred.draft:'+id),a)).toBe('A 在另一标签运行期间保留的草稿');await oneStream(page);expect(posts).toEqual(['/api/sessions']);
  } finally {if(held)await held.release();await server.send('release-recording');await other?.close();await server.close();}
});

test('a successful creation from an earlier process stays a receipt until explicit current-process verification',async({page})=>{
  const server=await memoryServer({script:'tests/browser/overview_server.py'});let held;
  try {
    const a=await setup(page,server.origin),before=await runtime(page);
    const input=page.getByRole('textbox',{name:'消息',exact:true});await input.fill('重启也留在 A 的草稿');
    const posts=[];page.on('request',request=>{if(request.method()==='POST')posts.push(new URL(request.url()).pathname);});
    held=await holdCreate(page);await page.getByRole('button',{name:'新建会话并打开主对话',exact:true}).click();await held.ready;
    const b=held.body.session_id;await server.restart();
    await expect.poll(async()=>(await runtime(page)).instance).not.toBe(before.instance);
    await expect.poll(async()=>(await runtime(page)).connected).toBe(true);
    await held.release();
    const receipt=page.getByRole('region',{name:'新建会话回执',exact:true});await expect(receipt).toContainText(b);await expect(receipt).toContainText('进程已变化');
    await expect(receipt.getByRole('button',{name:'打开已创建会话',exact:true})).toBeDisabled();
    expect(await selected(page)).toBe(a);await expect(input).toHaveValue('重启也留在 A 的草稿');await oneStream(page);
    await receipt.getByRole('link',{name:'查看已创建会话',exact:true}).click();
    const preview=page.getByRole('region',{name:'会话只读预览'});await expect(preview).toContainText('此分区暂无已读记录');
    expect(await selected(page)).toBe(a);
    await preview.getByRole('button',{name:'继续此会话',exact:true}).click();await expect.poll(()=>selected(page)).toBe(b);
    await expect(receipt).toHaveCount(0);await expect(input).toHaveValue('');
    expect(await page.evaluate(id=>sessionStorage.getItem('alfred.draft:'+id),a)).toBe('重启也留在 A 的草稿');expect(posts).toEqual(['/api/sessions']);await oneStream(page);
  } finally {if(held)await held.release();await server.close();}
});

for(const intent of ['unchanged','navigate','focus'])test(`first native mount before the real creation receipt preserves ${intent} intent`,async({page})=>{
  const server=await memoryServer({script:'tests/browser/overview_server.py'});let held,releaseSSE;
  try {
    await observeStream(page);
    const sseGate=new Promise(resolve=>releaseSSE=resolve);
    await page.route(/\/api\/events(?:\?.*)?$/,async route=>{await sseGate;await route.continue().catch(()=>{});});
    const posts=[];page.on('request',request=>{if(request.method()==='POST')posts.push(new URL(request.url()).pathname);});
    await page.goto(server.origin+'/inbox');held=await holdCreate(page);
    await page.getByRole('button',{name:'新建会话',exact:true}).click();await held.ready;
    expect(await selected(page)).toBeNull();await expect(page.locator('#page .page-body')).toHaveCount(0);
    if(intent==='navigate')await page.getByRole('navigation',{name:'主导航',exact:true}).getByRole('link',{name:'数据库',exact:true}).click();
    releaseSSE();
    await expect(page.getByRole('heading',{name:intent==='navigate'?'数据库':'收件箱',exact:true})).toBeVisible();
    await expect.poll(async()=>(await runtime(page)).connected).toBe(true);
    if(intent==='navigate')await page.getByRole('textbox',{name:'SQL',exact:true}).fill('SELECT 87 AS untouched_draft');
    if(intent==='focus') {
      // A real keyboard focus move, not a test-time focus correction.
      await page.keyboard.press('Tab');
      expect(await page.evaluate(()=>document.activeElement?.tagName)).not.toMatch(/^(BODY|H1)$/);
    }
    const focused=await page.locator(':focus').elementHandle();
    const b=held.body.session_id;await held.release();
    const input=page.getByRole('textbox',{name:'消息',exact:true});
    if(intent==='unchanged') {
      await expect.poll(()=>selected(page)).toBe(b);await expect(input).toBeEnabled();await expect(input).toBeFocused();
      await expect(page.getByRole('region',{name:'新建会话回执',exact:true})).toHaveCount(0);
    } else {
      const receipt=page.getByRole('region',{name:'新建会话回执',exact:true});await expect(receipt).toContainText(b);await expect(receipt).toContainText('新的页面或阅读意图');
      expect(await selected(page)).toBeNull();await expect(input).toBeDisabled();
      expect(await page.evaluate(expected=>document.activeElement===expected,focused)).toBe(true);
      if(intent==='navigate')await expect(page.getByRole('textbox',{name:'SQL',exact:true})).toHaveValue('SELECT 87 AS untouched_draft');
    }
    await oneStream(page);expect(posts).toEqual(['/api/sessions']);
  } finally {releaseSSE?.();if(held)await held.release();await server.close();}
});

for(const viewport of [{width:320,height:360},{width:320,height:800},{width:1440,height:360}])test(`a delayed receipt keeps the editing controls reachable at ${viewport.width}x${viewport.height}`,async({page},testInfo)=>{
  const server=await memoryServer({script:'tests/browser/overview_server.py'});let held;
  try {
    await page.setViewportSize(viewport);await observeStream(page);await page.goto(server.origin+'/inbox');
    if(viewport.width<1100)await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    await page.getByRole('button',{name:'新建会话',exact:true}).click();
    const input=page.getByRole('textbox',{name:'消息',exact:true}),send=page.getByRole('button',{name:'发送',exact:true});
    await expect(input).toBeEnabled();await expect.poll(async()=>(await runtime(page)).connected).toBe(true);
    const a=await selected(page);await input.fill('原会话草稿');
    const posts=[];page.on('request',request=>{if(request.method()==='POST')posts.push(new URL(request.url()).pathname);});
    held=await holdCreate(page);await page.getByRole('button',{name:'新建会话',exact:true}).click();await held.ready;
    await input.fill('201 回执等待期间输入的新草稿');await input.evaluate(element=>element.setSelectionRange(2,8));
    await expect(input).toBeInViewport({ratio:1});await expect(send).toBeInViewport({ratio:1});
    const b=held.body.session_id;await held.release();
    const receipt=page.getByRole('region',{name:'新建会话回执',exact:true});await expect(receipt).toContainText(b);await expect(receipt).toContainText('已有新的页面或阅读意图');
    await expect(input).toBeFocused();expect(await input.evaluate(element=>[element.selectionStart,element.selectionEnd])).toEqual([2,8]);
    await expect(input).toBeInViewport({ratio:1});await expect(send).toBeInViewport({ratio:1});
    await expect(input).toHaveValue('201 回执等待期间输入的新草稿');expect(await selected(page)).toBe(a);
    await expect(send).toBeEnabled();await expect(page.getByRole('button',{name:'收起主对话',exact:true})).toBeInViewport({ratio:1});
    await page.screenshot({path:testInfo.outputPath('receipt-before-scroll.png')});
    const status=page.locator('#shell-status'),open=receipt.getByRole('button',{name:'打开已创建会话',exact:true}),inspect=receipt.getByRole('link',{name:'查看已创建会话',exact:true});
    const statusBox=await status.boundingBox();await page.mouse.move(statusBox.x+statusBox.width/2,statusBox.y+statusBox.height/2);await page.mouse.wheel(0,1200);
    await expect(open).toBeInViewport({ratio:1});await expect(inspect).toBeInViewport({ratio:1});
    await expect(input).toBeFocused();expect(await input.evaluate(element=>[element.selectionStart,element.selectionEnd])).toEqual([2,8]);
    await page.screenshot({path:testInfo.outputPath('receipt-after-wheel.png')});
    // Reach Send and receipt actions with real keyboard input; no focus repair.
    await page.keyboard.press('Tab');await expect(send).toBeFocused();await expect(send).toBeInViewport({ratio:1});
    await send.click({trial:true});
    for(let step=0;step<12&&!(await inspect.evaluate(element=>element===document.activeElement));step++)await page.keyboard.press('Shift+Tab');
    await expect(inspect).toBeFocused();await expect(inspect).toBeInViewport({ratio:1});
    await page.keyboard.press('Shift+Tab');await expect(open).toBeFocused();await expect(open).toBeInViewport({ratio:1});
    await page.getByRole('button',{name:'收起主对话',exact:true}).click();await expect(page.locator('#mainbar')).toBeHidden();
    if(viewport.width<1100){await page.locator('#shell-toolbar [data-open-panel="navigation"]').click();await expect(page.getByRole('navigation',{name:'主导航',exact:true}).getByRole('link',{name:'收件箱',exact:true})).toBeInViewport({ratio:1});}
    expect(posts).toEqual(['/api/sessions']);await oneStream(page);
  } finally {if(held)await held.release();await server.close();}
});
