import {test, expect} from '@playwright/test';
import {controlledTransport, domain, emit, run, state} from './transport.js';

// Only the frozen public MainBar action port is called. HTTP and SSE remain
// controlled boundaries; the composed real read model is verified by S01/S03.
async function locate(page, session, run='early') {
  return page.evaluate(async ({session,run}) => {
    const {dashboard}=await import('/assets/app.js');
    return dashboard.locateReply({process_instance_id:'test-process',session_id:session,run_id:run,action_id:'explicit-'+run});
  },{session,run});
}
function record(session,extra={}) {
  return {process_instance_id:'test-process',session_id:session,run_id:'early',purpose:'chat',item_key:session+':early',activity_revision:1,created_at:null,source:'unrecorded_projection',user:{availability:'preview',blocks:null,preview:'安全请求预览'},reply_text:'早期完整正文',reply_disposition:'reply',recording_state:'failed',recording_source:'projection',history_contiguous:false,...extra};
}

test('exact MainBar location is one bounded read with a marked preview and separate history cursor',async({page})=>{
  const session=await controlledTransport(page);let reads=0;
  await page.route('**/api/mainbar/locate?*',route=>{reads++;return route.fulfill({json:record(session)});});
  expect((await locate(page,session)).status).toBe('applied');
  await expect(page.locator('#reading-status')).toContainText('相邻历史尚未读取');
  await expect(page.getByText('请求预览（未取得完整请求）',{exact:true})).toBeVisible();
  await expect(page.locator('#messages')).toContainText('回复已收到但未保存');
  await expect(page.locator('#messages [data-run-id="early"]')).toHaveCount(1);
  expect(reads).toBe(1);
  await page.getByRole('button',{name:'回到最新',exact:true}).click();
  await expect(page.locator('#reading-status')).toHaveText('');
  expect(reads).toBe(1);
});

test('process-gap navigation distinguishes a legal empty Run ID from an absent target',async({page})=>{
  const session=await controlledTransport(page);
  await emit(page,'transport_notice',{code:'deltas_dropped'});
  await expect(page.locator('#shell-status').getByRole('link',{name:'查看过程缺口',exact:true})).toHaveCount(0);
  await emit(page,'state_patch',state(session,2,{coordinator_state:'running',active_run:run(session,{run_id:''})}));
  await emit(page,'transport_notice',{code:'deltas_dropped'});
  await expect(page.locator('#shell-status').getByRole('link',{name:'查看过程缺口',exact:true})).toHaveAttribute('href','/runs/');
});

test('retired location cannot leave a loading state or late content after page navigation',async({page})=>{
  const session=await controlledTransport(page);let release,entered;
  const gate=new Promise(r=>release=r),received=new Promise(r=>entered=r);
  await page.route('**/api/mainbar/locate?*',async route=>{entered();await gate;await route.fulfill({json:record(session)}).catch(()=>{});});
  const request=locate(page,session);await received;
  await page.locator('nav a[href="/database"]').click();
  const sql=page.getByRole('textbox',{name:'SQL',exact:true});await sql.fill('SELECT 41');
  release();expect((await request).status).toBe('retired');
  await expect(page.locator('#reading-status')).not.toContainText('正在定位');
  await expect(page.locator('#messages')).not.toContainText('早期完整正文');
  await expect(sql).toBeFocused();
});

test('failed exact location has an explicit read-only retry and does not create a Run',async({page})=>{
  const session=await controlledTransport(page);let reads=0,posts=0;
  page.on('request',request=>{if(request.method()==='POST')posts++;});
  await page.route('**/api/mainbar/locate?*',route=>{reads++;return reads===1?route.fulfill({status:503,json:{code:'reply_unavailable'}}):route.fulfill({json:record(session)});});
  expect((await locate(page,session)).status).toBe('unavailable');
  await page.getByRole('button',{name:'收起主对话',exact:true}).click();
  await expect(page.locator('#shell-status')).toContainText('正文未完整加载，可只读重试');
  await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  await page.getByRole('button',{name:'重新定位正文',exact:true}).click();
  await expect(page.locator('#messages')).toContainText('早期完整正文');
  expect(reads).toBe(2);expect(posts).toBe(0);
});

test('a later unavailable request cannot erase a retained preview marker',async({page})=>{
  const session=await controlledTransport(page);let reads=0;
  await page.route('**/api/mainbar/locate?*',route=>{reads++;return route.fulfill({json:record(session,reads===1?{}:{user:{availability:'unavailable',blocks:null,preview:null}})});});
  await locate(page,session);await locate(page,session);
  const article=page.locator('#messages [data-run-id="early"]');
  await expect(article).toContainText('安全请求预览');
  await expect(article).toContainText('请求预览（未取得完整请求）');
});

test('hidden no-reply failure retains outcome attention without inventing a new reply',async({page})=>{
  const session=await controlledTransport(page);
  await page.getByRole('button',{name:'收起主对话',exact:true}).click();
  await domain(page,1,session,{name:'run.finished',outcome:'failed',reply_disposition:'no_reply',error:'明确失败'});
  await expect(page.locator('#shell-status')).toContainText('受控失败');
  await expect(page.locator('#shell-status')).not.toContainText('有新回复');
  await emit(page,'state_patch',state(session,2));
  await expect(page.locator('#shell-status')).toContainText('受控失败');
});

test('restored unread identity remains unconfirmed when another history item is visible',async({page})=>{
  const session=await controlledTransport(page);
  await page.evaluate(session=>sessionStorage.setItem('alfred.unread:'+session,JSON.stringify({run_id:'unavailable-target',seen:false})),session);
  await page.route('**/api/mainbar?*',route=>route.fulfill({json:{items:[{type:'run_pair',run_id:'different-run',activity_revision:1,user:[{type:'text',text:'别的请求'}],assistant:[{type:'text',text:'别的正式回复'}]}],next_cursor:null,runs_pending:false}}));
  await page.reload();await emit(page,'state_patch',state(session,1));
  await expect(page.locator('#messages')).toContainText('别的正式回复');
  await expect(page.locator('#shell-status')).toContainText('结果需核对');
  expect(await page.evaluate(session=>JSON.parse(sessionStorage.getItem('alfred.unread:'+session)).seen,session)).toBe(false);
});

test('a later preview cannot downgrade the same target full request or recorded state',async({page})=>{
  const session=await controlledTransport(page);let reads=0;
  await page.route('**/api/mainbar/locate?*',route=>{reads++;return route.fulfill({json:record(session,reads===1?{user:{availability:'full',blocks:[{type:'text',text:'完整请求的不可丢失后半段'}],preview:null},recording_state:'recorded'}:{recording_state:'pending'})});});
  await locate(page,session);await locate(page,session);
  const article=page.locator('#messages [data-run-id="early"]');
  await expect(article).toContainText('完整请求的不可丢失后半段');
  await expect(article).toContainText('已保存');await expect(article).not.toContainText('请求预览');
  await expect(article).toHaveCount(1);
});

test('idle reconnect verifies the same Run and does not infer saving from an unrelated persisted pair',async({page})=>{
  const session=await controlledTransport(page);let reads=0;
  await domain(page,1,session,{name:'run.finished',outcome:'completed',reply_disposition:'reply',reply:{blocks:[{type:'text',text:'待核对回复'}]}});
  // Drain the setup/finished reads before counting this reconnect's one read.
  // Domain decoding is asynchronous; seeing its reply proves it was applied,
  // and the history control re-enables only after queued refreshes finish.
  await expect(page.locator('#messages')).toContainText('待核对回复');
  await expect(page.locator('#older')).toBeEnabled();
  await page.getByRole('button',{name:'收起主对话',exact:true}).click();
  await page.route('**/api/mainbar?*',route=>{reads++;return route.fulfill({json:{items:[{type:'run_pair',run_id:reads===1?'other-recorded-run':'r1',activity_revision:1,user:[{type:'text',text:'持久请求'}],assistant:[{type:'text',text:'持久回复'}]}],next_cursor:null,runs_pending:false}});});
  const sources=await page.evaluate(()=>window.sources.length);
  await page.evaluate(()=>{window.dispatchEvent(new Event('offline'));window.dispatchEvent(new Event('online'));});
  await expect.poll(()=>page.evaluate(()=>window.sources.length)).toBe(sources+1);
  await emit(page,'state_patch',state(session,20));
  await expect.poll(()=>reads).toBe(1);
  await expect(page.locator('#shell-status')).toContainText('记录状态待核对');
  await expect(page.locator('#shell-status')).not.toContainText('已保存');
  await expect(page.locator('#shell-status')).not.toContainText('正在保存');
  await page.evaluate(async session=>{const {dashboard}=await import('/assets/app.js');await dashboard.selectSession(session);},session);
  await expect(page.locator('#shell-status')).toContainText('已保存');
  await expect(page.locator('#messages [data-run-id="r1"]')).toHaveCount(1);
  expect(reads).toBe(2);
});
