import {test,createChatSession,sendChat} from './chat-fixture.js';
import {expect} from '@playwright/test';

test('restored unread for real empty Session and Run IDs uses one guarded read and preserves its draft',async({page,chatServer})=>{
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
  await page.reload();
  const verify=page.getByRole('button',{name:'核对未读结果',exact:true});
  await expect(verify).toBeVisible();
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveValue('空ID会话草稿');
  await verify.click();
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
