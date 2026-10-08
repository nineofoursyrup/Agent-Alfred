import {test,expect} from '@playwright/test';

for(const boundary of ['entry','state'])test(`initial ${boundary} barrier preserves route history and mounts only the latest target after synchronization`,async({page})=>{
  const entry=await (await page.request.get('/api/entry')).json();
  const created=await page.request.post('/api/sessions',{headers:{'x-agent-alfred-csrf':entry.csrf_token},data:{}});
  expect(created.status()).toBe(201);const session=(await created.json()).session_id;
  await page.addInitScript(session=>{
    sessionStorage.setItem('alfred.session',session);sessionStorage.setItem('alfred.draft:'+session,'首次同步期间保留的草稿');
    const Native=window.EventSource;window.startupSources=[];window.startupStates=0;
    window.EventSource=class extends Native{constructor(url){super(url);window.startupSources.push(this);this.addEventListener('state_patch',()=>window.startupStates++);}};
  },session);
  let release,entered;const gate=new Promise(resolve=>release=resolve),held=new Promise(resolve=>entered=resolve);
  // The real entry or initial EventSource HTTP delivery is paused; production
  // Host snapshot creation, SSE parsing and state qualification are unchanged.
  await page.route(boundary==='entry'?/\/api\/entry$/:/\/api\/events(?:\?.*)?$/,async route=>{entered();await gate;await route.continue();},{times:1});
  const reads=[],posts=[];
  page.on('request',request=>{
    const path=new URL(request.url()).pathname;
    if(request.method()==='POST')posts.push(path);
    if(path==='/api/models'||path==='/api/connections'||path.startsWith('/api/overview/'))reads.push(path);
  });
  try{
    await page.goto('/overview');await held;
    await page.locator('nav a[href="/models"]').click();await expect(page).toHaveURL(/\/models$/);
    await page.locator('nav a[href="/connections"]').click();await expect(page).toHaveURL(/\/connections$/);
    await page.goBack();await expect(page).toHaveURL(/\/models$/);
    await page.goBack();await expect(page).toHaveURL(/\/overview$/);
    await page.goForward();await expect(page).toHaveURL(/\/models$/);
    await expect(page.locator('#page .page-body')).toHaveCount(0);
    expect(reads).toEqual([]);expect(posts).toEqual([]);
    expect(await page.evaluate(()=>window.startupStates)).toBe(0);
    await expect(page.locator('#message')).toHaveValue('首次同步期间保留的草稿');
    release();
    await expect(page.getByRole('heading',{name:'模型',exact:true})).toBeVisible();
    await expect(page.getByRole('textbox',{name:'显示名',exact:true}).first()).toBeVisible();
    expect(reads).toEqual(['/api/models']);expect(posts).toEqual([]);
    expect(await page.evaluate(()=>window.startupSources.length)).toBe(1);
    await expect(page.locator('#mainbar')).toHaveCount(1);
    await expect(page.locator('#message')).toHaveValue('首次同步期间保留的草稿');
    await page.locator('nav a[href="/models"]').click();expect(reads).toEqual(['/api/models']);
    // First readiness is a document latch: later offline reads remain lawful.
    await page.evaluate(()=>window.dispatchEvent(new Event('offline')));
    const connection=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/connections');
    await page.locator('nav a[href="/connections"]').click();expect((await connection).status()).toBe(200);
    await expect(page.getByRole('heading',{name:'连接',exact:true})).toBeVisible();
    expect(reads).toEqual(['/api/models','/api/connections']);expect(posts).toEqual([]);
  }finally{release();}
});

test('first readiness waits for a pending native panel-history transition before mounting its target',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  let release,entered;const gate=new Promise(resolve=>release=resolve),held=new Promise(resolve=>entered=resolve);
  await page.addInitScript(()=>{
    const Native=window.EventSource;window.startupStates=0;
    window.EventSource=class extends Native{constructor(url){super(url);this.addEventListener('state_patch',()=>window.startupStates++);}};
  });
  await page.route(/\/api\/entry$/,async route=>{entered();await gate;await route.continue();},{times:1});
  const reads=[];page.on('request',request=>{const path=new URL(request.url()).pathname;if(path==='/api/models'||path.startsWith('/api/overview/'))reads.push(path);});
  try{
    await page.goto('/overview');await held;
    await page.locator('#shell-toolbar [data-open-panel="navigation"]').click();
    // Pause delivery of the native Back call, then execute that same call below.
    // This models readiness arriving while panel history is still in flight.
    await page.evaluate(()=>{const back=history.back.bind(history);history.back=()=>{history.back=back;window.releaseStartupBack=back;window.startupBackHeld=true;};});
    await page.locator('nav a[href="/models"]').click();
    await expect.poll(()=>page.evaluate(()=>window.startupBackHeld)).toBe(true);
    release();await expect.poll(()=>page.evaluate(()=>window.startupStates)).toBeGreaterThan(0);
    await expect(page.locator('#page .page-body')).toHaveCount(0);expect(reads).toEqual([]);
    await page.evaluate(()=>window.releaseStartupBack());
    await expect(page).toHaveURL(/\/models$/);
    await expect(page.getByRole('textbox',{name:'显示名',exact:true}).first()).toBeVisible();
    expect(reads).toEqual(['/api/models']);await expect(page.locator('#page .page-body')).toHaveCount(1);
  }finally{release();}
});
