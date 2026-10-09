import {test,createChatSession,sendChat} from './chat-fixture.js';
import {expect} from '@playwright/test';

for (const beforeMount of ['same-page','navigate']) test(
  'restored unread for real empty Session and Run IDs uses one guarded read and preserves its draft: '+beforeMount,
  async({page,chatServer})=>{
  await page.goto(chatServer.origin+'/inbox');
  await createChatSession(page);
  const accepted=await sendChat(page,chatServer,'合法历史空身份的请求');
  await chatServer.send('legacy-empty-identities '+accepted.run_id);
  await page.evaluate(()=>{
    sessionStorage.setItem('alfred.session','');
    sessionStorage.setItem('alfred.draft:','空ID会话草稿');
    sessionStorage.setItem('alfred.unread:',JSON.stringify({run_id:'',seen:false}));
  });
  // Only fail the initial ordinary-history IO. Exact location, identity,
  // durable content and state validation still use the real HTTP service.
  await page.route('**/api/mainbar?*',route=>route.abort('failed'),{times:1});
  const reads=[],posts=[];
  page.on('request',request=>{if(new URL(request.url()).pathname==='/api/mainbar/locate')reads.push(request.url());if(request.method()==='POST')posts.push(request.url());});
  let releaseEvents, releaseLocation, receivedLocation, releaseNewLocation, receivedNewLocation, oldHandled;
  const newLocationGate=new Promise(resolve=>releaseNewLocation=resolve);
  const newLocationReady=new Promise(resolve=>receivedNewLocation=resolve);
  const oldLocationHandled=new Promise(resolve=>oldHandled=resolve);
  const failures=[];page.on('requestfailed',request=>{if(new URL(request.url()).pathname==='/api/mainbar/locate')failures.push(request.failure()?.errorText);});
  let heldLocations=0, oldDelivery;
  const eventsGate=new Promise(resolve=>releaseEvents=resolve);
  const locationGate=new Promise(resolve=>releaseLocation=resolve);
  const locationReady=new Promise(resolve=>receivedLocation=resolve);
  await page.route(/\/api\/events(?:\?.*)?$/, async route=>{await eventsGate;await route.continue().catch(()=>{});},{times:1});
  await page.route('**/api/mainbar/locate?*',async route=>{
    const response=await route.fetch();expect(response.status()).toBe(200);
    const first=++heldLocations===1;
    if(first){receivedLocation();await locationGate;}else{receivedNewLocation();await newLocationGate;}
    try {await route.fulfill({response});if(first)oldDelivery='delivered';}
    catch(failure) {if(first)oldDelivery='cancelled';else throw failure;}
    finally {if(first)oldHandled();}
  });
  await page.reload();
  const verify=page.getByRole('button',{name:'核对未读结果',exact:true});
  await expect(verify).toBeVisible();
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveValue('空ID会话草稿');
  await verify.click();await locationReady;
  await expect(page.locator('#reading-status')).toContainText('正在定位');
  if(beforeMount==='navigate') {
    // Navigation must retire immediately, even before the first page owner
    // exists, and even if the old HTTP response never arrives.
    await page.locator('nav a[href="/database"]').click();
    await expect(page.locator('#reading-status')).not.toContainText('正在定位');
    await expect.poll(()=>failures.length).toBe(1);
    releaseEvents();
    await expect(page.getByRole('heading',{name:'数据库',exact:true})).toBeVisible();
    const next=page.evaluate(async()=>{
      const {dashboard}=await import('/assets/app.js');
      return dashboard.locateReply({process_instance_id:dashboard.runtime().instance,session_id:'',run_id:'',action_id:'explicit-after-navigation'});
    });
    await newLocationReady;
    const sql=page.getByRole('textbox',{name:'SQL',exact:true});await sql.fill('SELECT 73');
    releaseLocation();await oldLocationHandled;
    // The cancelled old request must not clear a newer pending location.
    await expect(page.locator('#reading-status')).toContainText('正在定位');
    await expect(page.locator('#messages [data-run-id=""]')).toHaveCount(0);
    await expect(sql).toBeFocused();
    releaseNewLocation();expect((await next).status).toBe('applied');
    await expect(page.locator('#messages [data-run-id=""]')).toContainText('合法历史空身份的请求');
    await expect(page.locator('#messages [data-run-id=""]')).toContainText('已保存');
    await expect(sql).toBeFocused();
    await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveValue('空ID会话草稿');
    expect(posts).toHaveLength(0);expect(reads).toHaveLength(2);expect(failures).toHaveLength(1);
    return;
  }
  // First automatic page mount is presentation readiness, not a new MainBar
  // location intent. Finish it while the real exact-location response waits.
  releaseEvents();
  await expect(page.getByRole('heading',{name:'收件箱',exact:true})).toBeVisible();
  releaseLocation();await oldLocationHandled;expect(oldDelivery).toBe('delivered');
  await expect.poll(()=>reads.length).toBe(1);
  const query=new URL(reads[0]).searchParams;
  expect(query.has('session_id')).toBe(true);expect(query.get('session_id')).toBe('');
  expect(query.has('run_id')).toBe(true);expect(query.get('run_id')).toBe('');
  const article=page.locator('#messages [data-run-id=""]');
  await expect(article).toHaveCount(1);
  await expect(article).toContainText('合法历史空身份的请求');
  await expect(article).toContainText('离线模型回复');
  await expect(article).toContainText('已保存');
  await expect(page.locator('#reading-status')).toContainText('已定位历史回复');
  // The failed ordinary read still leaves latest-history continuity unknown.
  // Explicitly reread that same (empty) Session before acknowledging latest.
  const reread=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/mainbar');
  await page.evaluate(async()=>{const {dashboard}=await import('/assets/app.js');await dashboard.selectSession('');});
  await (await reread).finished();
  await page.getByRole('button',{name:'回到最新',exact:true}).click();
  await expect(page.locator('#shell-status')).not.toContainText('有新回复');
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveValue('空ID会话草稿');
  expect(posts).toHaveLength(0);
});
