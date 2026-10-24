# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: mainbar-integration.spec.js >> real S01 records compose with S02 exact location, identity merge and cross-Session drafts
- Location: tests/browser/mainbar-integration.spec.js:3:1

# Error details

```
Error: expect(locator).toHaveValue(expected) failed

Locator:  getByRole('textbox', { name: '消息', exact: true })
Expected: "B独立草稿"
Received: "A未提交草稿"
Timeout:  4000ms

Call log:
  - Expect "toHaveValue" getByRole('textbox', { name: '消息', exact: true }) with timeout 4000ms
  - waiting for getByRole('textbox', { name: '消息', exact: true })
    12 × locator resolved to <textarea rows="3" id="message" placeholder="写下消息；/skills 指定流程…"></textarea>
       - unexpected value "A未提交草稿"

```

```yaml
- textbox "消息":
  - /placeholder: 写下消息；/skills 指定流程…
  - text: A未提交草稿
```

# Test source

```ts
  1  | import {test,expect} from '@playwright/test';
  2  |
  3  | test('real S01 records compose with S02 exact location, identity merge and cross-Session drafts',async({page})=>{
  4  |   const writes=[],reads=[];
  5  |   page.on('request',request=>{
  6  |     const path=new URL(request.url()).pathname;
  7  |     if(request.method()==='POST')writes.push(path);
  8  |     if(path==='/api/mainbar/locate')reads.push(request.url());
  9  |   });
  10 |   await page.goto('/inbox');
  11 |   async function create(){
  12 |     const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/sessions'&&r.request().method()==='POST');
  13 |     await page.getByRole('button',{name:'新建会话',exact:true}).click();
  14 |     const created=await response;expect(created.status()).toBe(201);
  15 |     await expect(page.getByRole('textbox',{name:'消息',exact:true})).toBeEnabled();
  16 |     return (await created.json()).session_id;
  17 |   }
  18 |   async function send(text){
  19 |     await page.getByRole('textbox',{name:'消息',exact:true}).fill(text);
  20 |     const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/runs'&&r.request().method()==='POST');
  21 |     await page.getByRole('button',{name:'发送',exact:true}).click();
  22 |     const accepted=await response;expect(accepted.status()).toBe(202);
  23 |     const {run_id}=await accepted.json();
  24 |     await expect(page.locator(`#messages [data-run-id="${run_id}"]`)).toContainText('已保存');
  25 |     return run_id;
  26 |   }
  27 |   async function locate(session,run){
  28 |     const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/mainbar/locate');
  29 |     const result=await page.evaluate(async target=>{const {dashboard}=await import('/assets/app.js');return dashboard.locateReply({...target,process_instance_id:dashboard.runtime().instance,action_id:crypto.randomUUID()});},{session_id:session,run_id:run});
  30 |     expect(result.status).toBe('applied');
  31 |     const actual=await response;expect(actual.status()).toBe(200);
  32 |     const record=await actual.json();expect(record).toMatchObject({session_id:session,run_id:run,source:'recorded_pair',recording_state:'recorded',history_contiguous:false,user:{availability:'full'}});
  33 |     await expect(page.locator(`#messages [data-run-id="${run}"]`)).toHaveCount(1);
  34 |     await expect(page.locator('#reading-status')).toContainText('相邻历史尚未读取');
  35 |   }
  36 |   const sessionA=await create();
  37 |   const early=await send('S01与S02组合的早期请求');
  38 |   const latest=await send('另一条同文回复按Run身份保留');
  39 |   await page.getByRole('textbox',{name:'消息',exact:true}).fill('A未提交草稿');
  40 |   const before=writes.length;
  41 |   await locate(sessionA,early);
  42 |   expect(writes.length).toBe(before);
  43 |   await expect(page.locator(`#messages [data-run-id="${early}"]`)).toContainText('S01与S02组合的早期请求');
  44 |   await expect(page.locator(`#messages [data-run-id="${latest}"]`)).toContainText('离线模型回复');
  45 |   await page.getByRole('button',{name:'回到最新',exact:true}).click();
  46 |   await expect(page.locator('#reading-status')).toHaveText('');
  47 |   const sessionB=await create();expect(sessionB).not.toBe(sessionA);
  48 |   await page.getByRole('textbox',{name:'消息',exact:true}).fill('B独立草稿');
  49 |   const beforeCross=writes.length;
  50 |   await locate(sessionA,early);
  51 |   await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveValue('A未提交草稿');
  52 |   expect(writes.length).toBe(beforeCross);expect(reads).toHaveLength(2);
  53 |   expect(writes.filter(path=>path==='/api/runs')).toHaveLength(2);
  54 |   await page.evaluate(async session=>{const {dashboard}=await import('/assets/app.js');await dashboard.selectSession(session);},sessionB);
> 55 |   await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveValue('B独立草稿');
     |                                                                  ^ Error: expect(locator).toHaveValue(expected) failed
  56 | });
  57 |
```
