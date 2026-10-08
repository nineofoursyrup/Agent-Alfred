import {test,expect} from '@playwright/test';

test('real S01 records compose with S02 exact location, identity merge and cross-Session drafts',async({page})=>{
  const writes=[],reads=[];
  page.on('request',request=>{
    const path=new URL(request.url()).pathname;
    if(request.method()==='POST')writes.push(path);
    if(path==='/api/mainbar/locate')reads.push(request.url());
  });
  await page.goto('/inbox');
  async function create(){
    const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/sessions'&&r.request().method()==='POST');
    await page.getByRole('button',{name:'新建会话',exact:true}).click();
    const created=await response;expect(created.status()).toBe(201);
    await expect(page.getByRole('textbox',{name:'消息',exact:true})).toBeEnabled();
    return (await created.json()).session_id;
  }
  async function send(text){
    await page.getByRole('textbox',{name:'消息',exact:true}).fill(text);
    const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/runs'&&r.request().method()==='POST');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    const accepted=await response;expect(accepted.status()).toBe(202);
    const {run_id}=await accepted.json();
    await expect(page.locator(`#messages [data-run-id="${run_id}"]`)).toContainText('已保存');
    return run_id;
  }
  async function locate(session,run){
    const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/mainbar/locate');
    const result=await page.evaluate(async target=>{const {dashboard}=await import('/assets/app.js');return dashboard.locateReply({...target,process_instance_id:dashboard.runtime().instance,action_id:crypto.randomUUID()});},{session_id:session,run_id:run});
    expect(result.status).toBe('applied');
    const actual=await response;expect(actual.status()).toBe(200);
    const record=await actual.json();expect(record).toMatchObject({session_id:session,run_id:run,source:'recorded_pair',recording_state:'recorded',history_contiguous:false,user:{availability:'full'}});
    await expect(page.locator(`#messages [data-run-id="${run}"]`)).toHaveCount(1);
    await expect(page.locator('#reading-status')).toContainText('相邻历史尚未读取');
  }
  const sessionA=await create();
  const early=await send('S01与S02组合的早期请求');
  const latest=await send('另一条同文回复按Run身份保留');
  await page.getByRole('textbox',{name:'消息',exact:true}).fill('A未提交草稿');
  const before=writes.length;
  await locate(sessionA,early);
  expect(writes.length).toBe(before);
  await expect(page.locator(`#messages [data-run-id="${early}"]`)).toContainText('S01与S02组合的早期请求');
  await expect(page.locator(`#messages [data-run-id="${latest}"]`)).toContainText('离线模型回复');
  await page.getByRole('button',{name:'回到最新',exact:true}).click();
  await expect(page.locator('#reading-status')).toHaveText('');
  const sessionB=await create();expect(sessionB).not.toBe(sessionA);
  await page.getByRole('textbox',{name:'消息',exact:true}).fill('B独立草稿');
  const beforeCross=writes.length;
  await locate(sessionA,early);
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveValue('A未提交草稿');
  expect(writes.length).toBe(beforeCross);expect(reads).toHaveLength(2);
  expect(writes.filter(path=>path==='/api/runs')).toHaveLength(2);
  await page.evaluate(async session=>{const {dashboard}=await import('/assets/app.js');await dashboard.selectSession(session);},sessionB);
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveValue('B独立草稿');
});
