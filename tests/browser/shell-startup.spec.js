import {test,expect} from '@playwright/test';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {createInterface} from 'node:readline';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

test('a first native snapshot arriving after MainBar editing preserves focus and selection across the breakpoint',async({page},testInfo)=>{
  let release;const gate=new Promise(resolve=>release=resolve);
  const posts=[];page.on('request',request=>{if(request.method()==='POST')posts.push(new URL(request.url()).pathname);});
  // Hold actual SSE requests, including the replacement after explicit Session
  // creation. The real snapshot and shell/media-query handlers stay unchanged.
  await page.route(/\/api\/events(?:\?.*)?$/,async route=>{await gate;await route.continue();});
  const observations=[];
  const observe=async label=>observations.push({label,...await page.evaluate(()=>({active:document.activeElement?.id||document.activeElement?.tagName,panel:document.body.dataset.panel,layout:document.body.dataset.layout,mainbarHidden:document.getElementById('mainbar').hidden,pages:document.querySelectorAll('#page .page-body').length,selection:[document.getElementById('message').selectionStart,document.getElementById('message').selectionEnd]}))});
  try{
    await page.setViewportSize({width:1100,height:800});await page.goto('/inbox');
    await page.getByRole('button',{name:'新建会话',exact:true}).click();
    const input=page.getByRole('textbox',{name:'消息'});await input.fill('保留选区和当前输入');
    await input.evaluate(input=>input.setSelectionRange(2,5));
    await expect(page.locator('#page .page-body')).toHaveCount(0);await expect(input).toBeFocused();
    const preference=await page.evaluate(()=>sessionStorage.getItem('alfred.shell.wideOpen'));
    await expect(page.getByRole('button',{name:'发送',exact:true})).toBeDisabled();
    expect(await page.evaluate(async()=>(await import('/assets/app.js')).dashboard.runtime().connected)).toBe(false);
    await input.press('Enter');await page.evaluate(()=>new Promise(requestAnimationFrame));
    expect(posts).toEqual(['/api/sessions']);await expect(input).toHaveValue('保留选区和当前输入');
    expect(await input.evaluate(input=>[input.selectionStart,input.selectionEnd])).toEqual([2,5]);
    await observe('editing-before-first-snapshot');release();
    await expect(page.getByRole('heading',{name:'收件箱',exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'发送',exact:true})).toBeEnabled();expect(posts).toEqual(['/api/sessions']);
    await observe('first-page-mounted-before-resize');
    await page.setViewportSize({width:1099,height:800});
    await expect(page.locator('body')).toHaveAttribute('data-layout','narrow');
    await observe('narrow-after-media-change');
    await testInfo.attach('startup-breakpoint-observations',{body:JSON.stringify(observations,null,2),contentType:'application/json'});
    await expect(input).toBeVisible();await expect(input).toBeFocused();
    expect(await input.evaluate(input=>[input.selectionStart,input.selectionEnd])).toEqual([2,5]);
    await expect(input).toHaveValue('保留选区和当前输入');
    await page.setViewportSize({width:1100,height:800});await expect(input).toBeFocused();
    expect(await input.evaluate(input=>[input.selectionStart,input.selectionEnd])).toEqual([2,5]);
    expect(await page.evaluate(()=>sessionStorage.getItem('alfred.shell.wideOpen'))).toBe(preference);
  }finally{release();}
});

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
    await expect(page.locator("[data-model='opencode-go:deepseek-v4-flash']")).toBeVisible();
    // Models fields are mounted inside the initially collapsed details section.
    await expect(page.getByRole('textbox',{name:'显示名',exact:true,includeHidden:true}).first()).toBeAttached();
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
    await expect(page.locator("[data-model='opencode-go:deepseek-v4-flash']")).toBeVisible();
    // Models fields are mounted inside the initially collapsed details section.
    await expect(page.getByRole('textbox',{name:'显示名',exact:true,includeHidden:true}).first()).toBeAttached();
    expect(reads).toEqual(['/api/models']);await expect(page.locator('#page .page-body')).toHaveCount(1);
  }finally{release();}
});

test('same-revision reconnect finishes startup when matching credentials arrive offline after a real process restart',async({page,context})=>{
  const directory=await mkdtemp(join(tmpdir(),'alfred-startup-restart-'));
  const port=Number(process.env.ALFRED_BROWSER_TEST_PORT||17736)+2,origin=`http://127.0.0.1:${port}`;
  let child,lines;const exits=[];
  async function start(){
    let stderr='';child=spawn('.venv/bin/python',['tests/browser/server.py','--port',String(port),'--state',directory]);
    child.stderr.on('data',data=>stderr+=data);lines=createInterface({input:child.stdout});
    await Promise.race([new Promise(resolve=>lines.on('line',line=>{if(line==='ready')resolve();})),once(child,'exit').then(()=>{throw new Error(stderr);})]);
  }
  async function stop(){
    if(child&&child.exitCode===null){const done=once(child,'exit');child.kill('SIGTERM');await done;exits.push(child.exitCode);expect(child.exitCode).toBe(0);}
    lines?.close();
  }
  let releaseA=()=>{},releaseB=()=>{};
  const reads=[],posts=[],errors=[],identities=[];
  try{
    await start();
    const entry=await (await page.request.get(origin+'/api/entry')).json();
    const response=await page.request.post(origin+'/api/sessions',{headers:{'x-agent-alfred-csrf':entry.csrf_token},data:{}});
    expect(response.status()).toBe(201);const session=(await response.json()).session_id;
    await page.addInitScript(session=>{
      sessionStorage.setItem('alfred.session',session);sessionStorage.setItem('alfred.draft:'+session,'重连前的草稿');
      const Native=window.EventSource;window.startupSources=[];window.startupSnapshots=[];
      window.EventSource=class extends Native{constructor(url){super(url);window.startupSources.push(this);this.addEventListener('state_patch',event=>{const state=JSON.parse(event.data);window.startupSnapshots.push({instance:state.process_instance_id,revision:state.state_revision});});}};
    },session);
    let enteredA,enteredB;const heldA=new Promise(resolve=>enteredA=resolve),heldB=new Promise(resolve=>enteredB=resolve);
    const gateA=new Promise(resolve=>releaseA=resolve),gateB=new Promise(resolve=>releaseB=resolve);let entries=0;
    await page.route(/\/api\/entry$/,async route=>{
      const response=await route.fetch(),entry=await response.json();identities.push(entry.instance_id);
      if(++entries===1){enteredA();await gateA;}else if(entries===2){enteredB();await gateB;}
      await route.fulfill({response});
    });
    page.on('request',request=>{const path=new URL(request.url()).pathname;if(request.method()==='POST')posts.push(path);if(['/api/models','/api/connections'].includes(path))reads.push(path);});
    page.on('pageerror',error=>errors.push(String(error)));
    await page.goto(origin+'/models');await heldA;
    await stop();await start();releaseA();await heldB;
    const runtime=()=>page.evaluate(async()=>{const {dashboard}=await import('/assets/app.js');return dashboard.runtime();});
    await expect.poll(async()=>(await runtime()).revision).toBe(0);expect(identities[1]).not.toBe(identities[0]);
    await page.locator('nav a[href="/connections"]').click();await page.locator('nav a[href="/models"]').click();
    await expect(page.locator('#page .page-body')).toHaveCount(0);expect(reads).toEqual([]);expect(posts).toEqual([]);
    await context.setOffline(true);await expect.poll(async()=>(await runtime()).connected).toBe(false);
    const refreshed=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/entry');releaseB();await refreshed;
    await expect(page.getByRole('button',{name:'新建会话',exact:true})).toBeEnabled();
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    await page.locator('#message').fill('凭据已到，等待同修订重连时的新草稿');
    await expect(page.locator('#message')).toBeFocused();await expect(page.locator('#page .page-body')).toHaveCount(0);
    expect(reads).toEqual([]);expect(posts).toEqual([]);
    await context.setOffline(false);
    await expect.poll(()=>page.evaluate(()=>window.startupSnapshots.length)).toBe(2);
    await expect.poll(async()=>(await runtime()).connected).toBe(true);
    const snapshots=await page.evaluate(()=>window.startupSnapshots);
    expect(snapshots).toEqual([{instance:identities[1],revision:0},{instance:identities[1],revision:0}]);
    console.info('actual-startup-reconnect',JSON.stringify({runtime:await runtime(),snapshots,reads,posts,pages:await page.locator('#page .page-body').count()}));
    await expect(page.locator('#page .page-body')).toHaveCount(1);
    await expect(page.locator("[data-model='opencode-go:deepseek-v4-flash']")).toBeVisible();
    // Models fields are mounted inside the initially collapsed details section.
    await expect(page.getByRole('textbox',{name:'显示名',exact:true,includeHidden:true}).first()).toBeAttached();
    expect(reads).toEqual(['/api/models']);expect(posts).toEqual([]);expect(errors).toEqual([]);
    expect(await page.evaluate(()=>window.startupSources.filter(source=>source.readyState!==EventSource.CLOSED).length)).toBe(1);
    await expect(page.locator('#mainbar')).toHaveCount(1);
    await expect(page.locator('#message')).toHaveValue('凭据已到，等待同修订重连时的新草稿');await expect(page.locator('#message')).toBeFocused();
  }finally{
    releaseA();releaseB();await context.setOffline(false);await page.close();await stop();
    console.info('actual-startup-server-exits',JSON.stringify(exits));await rm(directory,{recursive:true,force:true});
  }
});
