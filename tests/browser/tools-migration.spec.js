import {test, expect} from '@playwright/test';
import {memoryServer} from './memory-server.js';
import {mcpServer, control} from './mcp-server.js';
import {createChatSession, sendChat} from './chat-fixture.js';
import {writeFile, readFile} from 'node:fs/promises';
import {join} from 'node:path';

const external = page => page.locator('article').filter({has:page.getByRole('heading',{name:'external_fixture',exact:true})});

test('S07 source groups use registered identity; collapse and readback retain draft, focus and order', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/ops_server.py'});
  try {
    await page.goto(server.origin+'/tools');
    const builtin = page.getByRole('region',{name:'内置工具',exact:true});
    await expect(builtin.getByRole('heading',{name:'read_persona',exact:true})).toBeVisible();
    await expect(builtin.getByRole('heading',{name:'external_fixture',exact:true})).toHaveCount(0);
    const unknown = page.getByRole('region',{name:'未知来源 · test-service',exact:true});
    await expect(unknown).toContainText('test-capability');
    const card = external(page), detail = card.getByRole('button',{name:'展开详情',exact:true});
    await expect(card.getByRole('combobox')).toBeHidden();
    await detail.click();
    await card.getByRole('combobox').selectOption('allowed');
    await card.getByRole('button',{name:'收起详情',exact:true}).click();
    await expect(card.getByRole('combobox')).toBeHidden();
    await expect(card.getByLabel('工具状态摘要')).toContainText('未保存');
    const order = await page.locator('.tool-row h3').allTextContents();
    await page.getByRole('button',{name:'核对当前授权',exact:true}).click();
    await expect(card.getByRole('combobox')).toBeHidden();
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    const select = card.getByRole('combobox');
    await expect(select).toHaveValue('allowed');
    await select.focus();
    const read = page.waitForResponse(r=>r.url().endsWith('/api/tools'));
    await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
    await read;
    await expect(select).toBeFocused();
    await expect(select).toHaveValue('allowed');
    expect(await page.locator('.tool-row h3').allTextContents()).toEqual(order);
    await expect(card.getByRole('link',{name:'连接维护',exact:true})).toHaveAttribute('href','/connections');
    await expect(card.getByRole('link',{name:'查看包含该工具的运行',exact:true})).toHaveAttribute('href','/ops?tool='+encodeURIComponent('["test-service","test-capability"]'));
  } finally {await server.close();}
});

test('S07 real MCP same-name replacement retires permission and draft; restored identity does not revive an old success', async ({page}) => {
  const server=await mcpServer(), origin=`http://127.0.0.1:${server.entry.port}`;
  try {
    const entry=await (await page.request.get(origin+'/api/entry')).json();
    async function maintain(action,server_key=null) {
      const before=await (await page.request.get(origin+'/api/connections')).json();
      const operation_id=crypto.randomUUID();
      const result=await page.request.post(origin+'/api/connections/mcp',{headers:{'x-agent-alfred-csrf':entry.csrf_token},data:{action,server_key,operation_id,token:before.mcp.token}});
      expect(result.status()).toBe(202);
      await expect.poll(async()=>(await (await page.request.get(origin+'/api/connections/mcp/operation?operation_id='+operation_id)).json()).status).toBe('completed');
    }
    await control(server,{servers:['test','twin']});await maintain('apply');
    await page.goto(origin+'/tools');
    const group=page.getByRole('region',{name:/^MCP · test ·/});
    const twin=page.getByRole('region',{name:/^MCP · twin ·/});
    await expect(group).toContainText('不是已授权数');
    await expect(twin).toContainText('可用工具 1 / 1');
    const card=group.locator('article');
    const original=await card.getAttribute('data-tool-identity');
    expect(await twin.locator('article').getAttribute('data-tool-identity')).not.toBe(original);
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    await expect(card).toContainText('原始工具名 echo');
    await card.getByRole('combobox').selectOption('allowed');
    await card.getByRole('button',{name:'保存授权',exact:true}).click();
    await expect(card).toContainText('授权已生效');
    await card.getByRole('combobox').selectOption('denied');
    await card.getByRole('button',{name:'收起详情',exact:true}).click();
    await control(server,{tools:[{name:'echo',description:'同名能力已更换',inputSchema:{type:'object'}}]});
    await maintain('reconnect','test');
    await page.getByRole('button',{name:'核对当前授权',exact:true}).click();
    await expect(card).not.toHaveAttribute('data-tool-identity',original);
    const retired=page.getByRole('region',{name:'历史不可调用目录',exact:true});
    await expect(retired).toContainText('原身份授权草稿已失效并清除');
    await expect(retired.getByRole('combobox')).toHaveCount(0);
    await expect(retired.getByRole('link',{name:'查看包含该工具的运行',exact:true})).toHaveAttribute('href','/ops?tool='+encodeURIComponent(original));
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    await expect(card.getByRole('combobox')).toHaveValue('unset');
    const replaced=await (await page.request.get(origin+'/api/tools')).json();
    expect(replaced.authorizations[original]).toBe('unset');
    await control(server,{tools:[{name:'echo',description:'External echo',inputSchema:{type:'object'}}]});
    await maintain('reconnect','test');
    await page.getByRole('button',{name:'核对当前授权',exact:true}).click();
    await expect(card).toHaveAttribute('data-tool-identity',original);
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    await expect(card.getByRole('combobox')).toHaveValue('unset');
    await expect(card).toContainText('模型暴露：hidden');
    await expect(card).not.toContainText('授权已生效');
    expect((await control(server)).requests.filter(r=>r.method==='tools/call')).toHaveLength(0);
  } finally {await server.close();}
});

test('S07 in-flight edits, lost receipt, focus and reconnect keep explicit comparison without reposting', async ({page,context}) => {
  const server=await memoryServer({script:'tests/browser/ops_server.py'});
  let release;
  try {
    await page.goto(server.origin+'/tools');
    const card=external(page);
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    let persisted;const arrived=new Promise(resolve=>persisted=resolve),gate=new Promise(resolve=>release=resolve);
    let posts=0;
    await page.route('**/api/tools/authorization',async route=>{posts++;const response=await route.fetch();persisted();await gate;await route.fulfill({response});});
    await card.getByRole('combobox').selectOption('allowed');
    await card.getByRole('button',{name:'保存授权',exact:true}).click();await arrived;
    await card.getByRole('combobox').selectOption('denied');
    await card.getByRole('button',{name:'收起详情',exact:true}).click();release();
    await expect(card.getByLabel('工具状态摘要')).toContainText('已保存授权：allowed');
    await expect(card.getByLabel('工具状态摘要')).toContainText('未保存草稿：denied');
    await expect(card.getByLabel('工具状态摘要')).toContainText('版本冲突');
    await card.getByRole('link',{name:'连接维护',exact:true}).click();
    await expect(page.getByRole('button',{name:'留在此页',exact:true})).toBeFocused();
    await page.getByRole('button',{name:'留在此页',exact:true}).click();
    await expect(page).toHaveURL(server.origin+'/tools');
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    await card.getByRole('button',{name:'基于当前版本继续编辑',exact:true}).click();
    await page.unroute('**/api/tools/authorization');
    await page.route('**/api/tools/authorization',async route=>{posts++;await route.fetch();await route.abort('failed');});
    await card.getByRole('button',{name:'保存授权',exact:true}).click();
    await expect(card).toContainText('提交结果尚未确认');
    await expect(card).toContainText('服务端当前保存值：denied');
    await card.getByRole('button',{name:'收起详情',exact:true}).click();
    await page.getByRole('button',{name:'核对当前授权',exact:true}).click();
    await expect(card).toContainText('提交结果尚未确认');
    await context.setOffline(true);
    await expect(page.getByText(/离线，当前生效状态待核验/)).toBeVisible();
    await expect(card.getByLabel('工具状态摘要')).toContainText('模型暴露：unverified');
    await context.setOffline(false);
    await expect(card.getByLabel('工具状态摘要')).toContainText('实际应用：applied');
    await expect(card).toContainText('提交结果尚未确认');
    await expect(card).not.toContainText('授权已生效');
    expect(posts).toBe(2);
  } finally {release?.();await context.setOffline(false);await server.close();}
});

test('S07 an already submitted authorization can leave without discarding input or cancelling the real command', async ({page}) => {
  const server=await memoryServer({script:'tests/browser/ops_server.py'});
  let release;
  try {
    await page.goto(server.origin+'/tools');
    const card=external(page);
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    let persisted;const arrived=new Promise(resolve=>persisted=resolve),gate=new Promise(resolve=>release=resolve);
    let posts=0;
    await page.route('**/api/tools/authorization',async route=>{posts++;const response=await route.fetch();persisted();await gate;await route.fulfill({response}).catch(()=>{});});
    await card.getByRole('combobox').selectOption('allowed');
    await card.getByRole('button',{name:'保存授权',exact:true}).click();await arrived;
    await card.getByRole('link',{name:'连接维护',exact:true}).click();
    await expect(page).toHaveURL(server.origin+'/connections');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    release();
    await page.getByRole('link',{name:'工具',exact:true}).click();
    await expect(card).toContainText('已保存授权：allowed');
    await expect(card).not.toContainText('服务端确认操作');
    expect(posts).toBe(1);
  } finally {release?.();await server.close();}
});

test('S07 Spec F1 a lost authorization receipt without newer input can leave after current readback', async ({page}) => {
  const server=await memoryServer({script:'tests/browser/ops_server.py'});
  try {
    await page.goto(server.origin+'/tools');
    const card=external(page),select=card.getByRole('combobox'),save=card.getByRole('button',{name:'保存授权',exact:true});
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    const posts=[];
    await page.route('**/api/tools/authorization',async route=>{
      const response=await route.fetch();posts.push({body:route.request().postDataJSON(),status:response.status()});
      await route.abort('failed');
    });
    await select.selectOption('allowed');await save.click();
    await expect(save).toBeEnabled();
    await expect(card).toContainText('提交结果尚未确认');
    await expect(card).toContainText('服务端当前保存值：allowed · 保存版本 1');
    await expect(card).toContainText('基线版本 0');
    await expect(card).not.toContainText('服务端确认操作');
    await page.getByRole('button',{name:'核对当前授权',exact:true}).click();
    await expect(select).toHaveValue('allowed');
    await expect(card).toContainText('提交结果尚未确认');
    await card.getByRole('link',{name:'连接维护',exact:true}).click();
    await expect(page).toHaveURL(server.origin+'/connections');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.getByRole('link',{name:'工具',exact:true}).click();
    await expect(card).toContainText('已保存授权：allowed');
    await expect(card).not.toContainText('服务端确认操作');
    expect(posts).toEqual([{body:{identity:'["test-service","test-capability"]',authorization:'allowed',expected_revision:0},status:200}]);
  } finally {await server.close();}
});

test('S07 Spec F1 a definite authorization refusal keeps unsubmitted input until explicit retry', async ({page}) => {
  const server=await memoryServer({script:'tests/browser/ops_server.py'});
  try {
    await page.goto(server.origin+'/tools');
    const card=external(page),select=card.getByRole('combobox'),save=card.getByRole('button',{name:'保存授权',exact:true});
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    const posts=[];
    await page.route('**/api/tools/authorization',async route=>{
      const response=await route.fetch();posts.push({body:route.request().postDataJSON(),status:response.status()});
      await route.fulfill({response});
    });
    await server.send('busy');await select.selectOption('allowed');await save.click();
    await expect(save).toBeEnabled();
    await expect(card).toContainText('保存未完成：mutation_in_flight');
    await expect(card.getByLabel('工具状态摘要')).toContainText('未保存草稿：allowed · 基线版本 0');
    await server.send('idle');
    await card.getByRole('link',{name:'连接维护',exact:true}).click();
    await expect(page.getByRole('button',{name:'留在此页',exact:true})).toBeFocused();
    await page.getByRole('button',{name:'留在此页',exact:true}).click();
    await expect(select).toHaveValue('allowed');expect(posts).toHaveLength(1);
    await save.click();await expect(save).toBeEnabled();
    await expect(card).toContainText('授权已生效');
    await card.getByRole('link',{name:'连接维护',exact:true}).click();
    await expect(page).toHaveURL(server.origin+'/connections');
    expect(posts.map(post=>[post.status,post.body.expected_revision])).toEqual([[409,0],[200,0]]);
  } finally {await server.send('idle');await server.close();}
});

test('S07 Spec F1 discarding only a successor draft retains the unknown submitted action', async ({page}) => {
  const server=await memoryServer({script:'tests/browser/ops_server.py'});
  try {
    await page.goto(server.origin+'/tools');
    const card=external(page),select=card.getByRole('combobox'),save=card.getByRole('button',{name:'保存授权',exact:true});
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    let posts=0;
    await page.route('**/api/tools/authorization',async route=>{posts++;expect((await route.fetch()).status()).toBe(200);await route.abort('failed');});
    await select.selectOption('allowed');await save.click();await expect(save).toBeEnabled();
    await expect(card).toContainText('提交结果尚未确认');
    await expect(card).toContainText('服务端当前保存值：allowed · 保存版本 1');
    await select.selectOption('unset');
    await expect(card.getByLabel('工具状态摘要')).toContainText('未保存草稿：unset · 基线版本 0 · 版本冲突');
    await card.getByRole('link',{name:'连接维护',exact:true}).click();
    await expect(page.getByRole('button',{name:'留在此页',exact:true})).toBeFocused();
    await page.getByRole('button',{name:'留在此页',exact:true}).click();
    await expect(select).toHaveValue('unset');
    await card.getByRole('button',{name:'基于当前版本继续编辑',exact:true}).click();
    await select.selectOption('allowed');
    await expect(card.getByLabel('工具状态摘要')).not.toContainText('未保存草稿');
    await card.getByRole('button',{name:'收起详情',exact:true}).click();
    await expect(card).toContainText('提交结果尚未确认');
    await expect(page.getByRole('button',{name:'核对当前授权',exact:true})).toBeEnabled();
    await expect(card.getByRole('link',{name:'查看包含该工具的运行',exact:true})).toHaveAttribute('href','/ops?tool='+encodeURIComponent('["test-service","test-capability"]'));
    await card.getByRole('link',{name:'连接维护',exact:true}).click();
    await expect(page).toHaveURL(server.origin+'/connections');expect(posts).toBe(1);
  } finally {await server.close();}
});

for(const receipt of ['success','missing','rejected']) test(`S07 F1 an in-flight edit back to the saved baseline remains a new draft (${receipt})`, async ({page}) => {
  const server=await memoryServer({script:'tests/browser/ops_server.py'});
  let release;
  try {
    await page.goto(server.origin+'/tools');
    const card=external(page),select=card.getByRole('combobox'),save=card.getByRole('button',{name:'保存授权',exact:true});
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    await expect(select).toHaveValue('unset');
    let captured;const arrived=new Promise(resolve=>captured=resolve),gate=new Promise(resolve=>release=resolve);
    const posts=[];
    await page.route('**/api/tools/authorization',async route=>{
      posts.push(route.request().postDataJSON());
      const response=await route.fetch();captured(response.status());await gate;
      if(receipt==='missing'&&posts.length===1)await route.abort('failed');
      else await route.fulfill({response});
    });
    if(receipt==='rejected')await server.send('busy');
    await select.selectOption('allowed');await save.click();
    expect(await arrived).toBe(receipt==='rejected'?409:200);
    await select.selectOption('unset');
    await card.getByRole('button',{name:'收起详情',exact:true}).click();
    await expect(card.getByLabel('工具状态摘要')).toContainText('未保存草稿：unset');
    if(receipt==='rejected')await server.send('idle');
    release();
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    await expect(save).toBeEnabled();
    await expect(select).toHaveValue('unset');
    await expect(card).toContainText('已保存授权：'+(receipt==='rejected'?'unset':'allowed'));
    await expect(card).toContainText(receipt==='missing'?'提交结果尚未确认':receipt==='rejected'?'保存未完成：mutation_in_flight':'服务端确认操作');
    await page.getByRole('button',{name:'核对当前授权',exact:true}).click();
    await expect(select).toHaveValue('unset');
    await card.getByRole('link',{name:'连接维护',exact:true}).click();
    await expect(page.getByRole('button',{name:'留在此页',exact:true})).toBeFocused();
    await page.getByRole('button',{name:'留在此页',exact:true}).click();
    await expect(select).toHaveValue('unset');expect(posts).toHaveLength(1);
    if(receipt==='success') {
      // Keeping the new input does not silently adopt the revision just saved.
      await save.click();await expect(card).toContainText('authorization_conflict');
      await expect(select).toHaveValue('unset');expect(posts[1].expected_revision).toBe(0);
      await card.getByRole('button',{name:'基于当前版本继续编辑',exact:true}).click();
      await save.click();await expect(save).toBeEnabled();
      await expect(card).toContainText('已保存授权：unset');
      await expect(card.getByLabel('工具状态摘要')).not.toContainText('未保存草稿');
      expect(posts).toHaveLength(3);expect(posts[2].expected_revision).toBe(1);
    } else {
      await card.getByRole('link',{name:'连接维护',exact:true}).click();
      await page.getByRole('button',{name:'放弃并离开',exact:true}).click();
      await expect(page).toHaveURL(server.origin+'/connections');
      await page.getByRole('link',{name:'工具',exact:true}).click();
      await expect(card.getByLabel('工具状态摘要')).not.toContainText('未保存草稿');
      await expect(card).not.toContainText('服务端确认操作');expect(posts).toHaveLength(1);
    }
  } finally {release?.();await server.send('idle');await server.close();}
});

test('S07 F2 a new edit after lost receipt and failed readback retains its original CAS baseline', async ({page}) => {
  const server=await memoryServer({script:'tests/browser/ops_server.py'});
  try {
    await page.goto(server.origin+'/tools');
    const card=external(page),select=card.getByRole('combobox'),save=card.getByRole('button',{name:'保存授权',exact:true});
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    await expect(select).toHaveValue('unset');
    const posts=[],statuses=[];
    let blockReads=true;
    await page.route('**/api/tools',route=>blockReads?route.fulfill({status:503,contentType:'application/json',body:'{"error":"readback_unavailable"}'}):route.continue());
    await page.route('**/api/tools/authorization',async route=>{
      posts.push(route.request().postDataJSON());
      const response=await route.fetch();statuses.push(response.status());
      if(posts.length===1)await route.abort('failed');
      else await route.fulfill({response});
    });
    await select.selectOption('allowed');await save.click();
    await expect(page.getByText(/当前授权状态无法核实；保留旧目录与草稿/)).toBeVisible();
    await expect(card).toContainText('提交结果尚未确认');
    const actual=await (await page.request.get(server.origin+'/api/tools')).json();
    expect(actual.revision).toBe(1);expect(actual.authorizations[posts[0].identity]).toBe('allowed');
    expect(statuses).toEqual([200]);expect(posts[0].expected_revision).toBe(0);
    // This edit occurs after both requests ended, when submission is no longer pending.
    await select.selectOption('unset');
    await card.getByRole('button',{name:'收起详情',exact:true}).click();
    await expect(card.getByLabel('工具状态摘要')).toContainText('未保存草稿：unset · 基线版本 0');
    await card.getByRole('link',{name:'连接维护',exact:true}).click();
    await expect(page.getByRole('button',{name:'留在此页',exact:true})).toBeFocused();
    await page.getByRole('button',{name:'留在此页',exact:true}).click();
    await card.getByRole('button',{name:'展开详情',exact:true}).click();
    await expect(select).toHaveValue('unset');await expect(save).toBeDisabled();
    blockReads=false;
    await page.getByRole('button',{name:'核对当前授权',exact:true}).click();
    await expect(save).toBeEnabled();
    await expect(select).toHaveValue('unset');
    await expect(card).toContainText('已保存授权：allowed');
    await expect(card.getByLabel('工具状态摘要')).toContainText('未保存草稿：unset · 基线版本 0 · 版本冲突');
    await expect(card).toContainText('提交结果尚未确认');expect(posts).toHaveLength(1);
    await save.click();await expect(card).toContainText('authorization_conflict');
    await expect(select).toHaveValue('unset');expect(posts[1].expected_revision).toBe(0);
    await card.getByRole('button',{name:'基于当前版本继续编辑',exact:true}).click();
    await save.click();await expect(save).toBeEnabled();
    await expect(card).toContainText('已保存授权：unset');
    await expect(card.getByLabel('工具状态摘要')).not.toContainText('未保存草稿');
    expect(posts).toHaveLength(3);expect(posts[2].expected_revision).toBe(1);
    expect(statuses).toEqual([200,409,200]);
  } finally {await server.close();}
});

test('S07 unreadable catalog keeps draft and local status; unavailable MCP is visible without inventing a connection', async ({page}) => {
  const server=await mcpServer(['invalid']),origin=`http://127.0.0.1:${server.entry.port}`;
  try {
    await page.goto(origin+'/tools');
    await expect(page.getByText(/MCP 目录：configuration_invalid/)).toBeVisible();
    const local=page.getByRole('region',{name:'内置工具',exact:true}).locator('article').filter({has:page.getByRole('heading',{name:'read_persona',exact:true})});
    await expect(local).toContainText('本地工具，不需要外部授权');
    await page.route('**/api/tools',route=>route.abort('failed'));
    await page.getByRole('button',{name:'核对当前授权',exact:true}).click();
    await expect(page.getByText(/当前授权状态无法核实；保留旧目录与草稿/)).toBeVisible();
    await expect(page.getByRole('button',{name:'重新应用已保存授权',exact:true})).toBeDisabled();
    await expect(local).toContainText('模型暴露：real');
    expect((await control(server)).requests).toHaveLength(0);
  } finally {await server.close();}
});

for(const override of [false,true]) test(`S07 Persona discovery and explicit ordinary Runs preserve version, conflict and next-Run application (override=${override})`, async ({page}) => {
  const server=await memoryServer({script:'tests/browser/tools_server.py',prepare:override?directory=>writeFile(join(directory,'external-persona.md'),'显式外部人格，禁止受管修改。'):undefined});
  try {
    await page.goto(server.origin+'/tools');
    const builtin=page.getByRole('region',{name:'内置工具',exact:true});
    for(const name of ['read_persona','update_persona']) {
      const row=builtin.locator('article').filter({has:page.getByRole('heading',{name,exact:true})});
      await expect(row).toContainText('不需要外部授权');
      await expect(row.getByRole('combobox')).toHaveCount(0);
    }
    await createChatSession(page);
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    const chat=page.getByRole('region',{name:'主对话'});
    const read=await sendChat(page,server,'人格：读取');
    await expect(chat).toContainText('"version"');
    await expect(chat).toContainText('"external_override": '+override);
    const updated=await sendChat(page,server,'人格：更新');
    if(override) {
      await expect(chat).toContainText('Explicit persona file is active');
      expect(await readFile(join(server.directory,'external-persona.md'),'utf8')).toBe('显式外部人格，禁止受管修改。');
    } else {
      await expect(chat).toContainText('"state": "complete"');
      await expect(chat).toContainText('本 Run 使用更新人格：false');
      await sendChat(page,server,'人格：核对下一 Run');
      await expect(chat).toContainText('本 Run 使用更新人格：true');
      await sendChat(page,server,'人格：冲突');
      await expect(chat).toContainText('Persona version conflict; read again.');
      await sendChat(page,server,'人格：读取');
      await expect(chat).toContainText('迁移验证人格；保留原有约束。');
      expect(await readFile(join(server.directory,'persona/persona.md'),'utf8')).toBe('迁移验证人格；保留原有约束。');
    }
    for(const run of [read,updated]) {
      const evidence=await (await page.request.get(server.origin+'/api/run-evidence?run_id='+run.run_id)).json();
      expect(evidence.trace_status).toBe('available');
      expect(JSON.stringify(evidence)).toContain(run.run_id);
      const catalog=await (await page.request.get(server.origin+'/api/tools')).json();
      const located=await page.request.get(server.origin+'/api/mainbar/locate?'+new URLSearchParams({process_instance_id:catalog.process_instance_id,session_id:run.session_id,run_id:run.run_id}));
      expect(located.status()).toBe(200);
      const record=await located.json();
      expect(record.recording_state).toBe('recorded');
    }
    await page.getByRole('link',{name:'运行',exact:true}).click();
    await expect(page.getByRole('heading',{name:'运行',exact:true})).toBeVisible();
  } finally {await server.close();}
});
