import {test, expect} from '@playwright/test';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {once} from 'node:events';

async function server(args=[]) {
  const child = spawn('.venv/bin/python', ['tests/browser/mcp_server.py',...args]);
  const lines = createInterface({input:child.stdout});
  let errors='';child.stderr.on('data',d=>errors+=d);
  const value=await Promise.race([once(lines,'line').then(([l])=>JSON.parse(l)),
    once(child,'exit').then(()=>{throw new Error(errors);})]);
  return {...value, async close(){const done=once(child,'exit');child.kill('SIGTERM');await done;lines.close();if(child.exitCode!==0)throw new Error(errors);}};
}
const control=async(s,body)=> (await fetch(s.control, body?{method:'POST',body:JSON.stringify(body)}:{})).json();

test('MCP CE-01 CE-05 CE-11 MainBar authorization, safe result and persistent cost',async({browser})=>{
  const s=await server();const context=await browser.newContext();
  try{
    const a=await context.newPage(),b=await context.newPage();const origin=`http://127.0.0.1:${s.entry.port}`;
    await a.goto(origin+'/connections');await b.goto(origin+'/tools');
    await expect(a.locator('[data-mcp-server="test"]')).toContainText('已连接');
    const tool=b.locator('article').filter({has:b.getByRole('heading',{name:'mcp_test_echo',exact:true})});
    await expect(tool).toContainText('模型暴露：hidden');
    expect((await control(s)).requests.filter(r=>r.method==='tools/call')).toHaveLength(0);
    await tool.getByRole('combobox').selectOption('allowed');await tool.getByRole('button',{name:'保存授权',exact:true}).click();
    await expect(tool).toContainText('模型暴露：real');
    await b.getByRole('button',{name:'新建会话',exact:true}).click();
    await b.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    await b.getByRole('textbox',{name:'消息'}).fill('MCP 验收');await b.getByRole('button',{name:'发送',exact:true}).click();
    const chat=b.getByRole('region',{name:'主对话'});
    await expect(chat).toContainText('<script>window.injected=true</script>');
    await expect(chat).not.toContainText('browser-old');
    expect(await b.evaluate(()=>window.injected)).toBeUndefined();
    await expect(chat).toContainText('不支持');
    expect((await control(s)).requests.filter(r=>r.method==='tools/call')).toHaveLength(1);
    await b.goto(origin+'/ops');await expect(b.getByRole('region',{name:'账目汇总'})).toContainText('未知');
  }finally{await context.close();await s.close();}
});

test('MCP CE-09 CE-14 double-tab env publication and explicit reconnect',async({browser})=>{
  const s=await server();const context=await browser.newContext();
  try{
    const a=await context.newPage(),b=await context.newPage();const origin=`http://127.0.0.1:${s.entry.port}`;
    await a.goto(origin+'/connections');await b.goto(origin+'/connections');
    await expect(a.locator('[data-mcp-server="test"]')).toContainText('已连接');
    const before=(await control(s)).requests.filter(r=>r.method==='initialize').length;
    await control(s,{token:'browser-new'});await b.getByRole('button',{name:'重新读取 .env'}).click();
    await expect(a.locator('[data-mcp-server="test"]')).toContainText('restart_required');
    await expect(b.locator('[data-mcp-server="test"]')).toContainText('restart_required');
    expect((await control(s)).requests.filter(r=>r.method==='initialize')).toHaveLength(before);
    await b.locator('[data-mcp-server="test"]').getByRole('button',{name:'重连',exact:true}).click();
    await expect(b.locator('[data-mcp-server="test"]')).toContainText('已连接');
    await expect(b.locator('[data-mcp-server="test"]').getByRole('button',{name:'重连',exact:true})).toBeFocused();
    await expect(a.locator('[data-mcp-server="test"]')).toContainText('已连接');
    expect((await control(s)).requests.filter(r=>r.method==='initialize')).toHaveLength(before+1);
  }finally{await context.close();await s.close();}
});

test('MCP SPEC-01 real HTTP recovery from invalid startup and stale preview',async({browser})=>{
  const s=await server(['invalid']);const context=await browser.newContext();
  try{
    const a=await context.newPage();const origin=`http://127.0.0.1:${s.entry.port}`;
    await a.goto(origin+'/connections');
    await expect(a.getByRole('region',{name:'MCP 服务器'})).toContainText('configuration_invalid');
    await control(s,{enabled:true});
    const staleApply = await a.getByRole('button',{name:'应用 mcp.json',exact:true}).elementHandle();
    await a.getByRole('button',{name:'应用 mcp.json',exact:true}).click();
    await expect(a.getByText('Error: mcp_conflict',{exact:true})).toBeVisible();
    expect((await control(s)).requests).toHaveLength(0);
    // The conflict notice precedes the refreshed preview and its new token.
    await expect.poll(() => staleApply.evaluate(node => node.isConnected)).toBe(false);
    await a.getByRole('button',{name:'应用 mcp.json',exact:true}).click();
    await expect(a.locator('[data-mcp-server="test"]')).toContainText('已连接');
    expect((await control(s)).requests.filter(r=>r.method==='initialize')).toHaveLength(1);
  }finally{await context.close();await s.close();}
});

test('MCP CE-09 CE-14 late old HTTP snapshots cannot restore callable UI',async({browser})=>{
  const s=await server();const context=await browser.newContext();let release;
  try{
    const a=await context.newPage(),b=await context.newPage();const origin=`http://127.0.0.1:${s.entry.port}`;
    await a.goto(origin+'/connections');await b.goto(origin+'/connections');
    await expect(a.locator('[data-mcp-server="test"]')).toContainText('已连接');
    let delivered;const finished=new Promise(r=>delivered=r);
    let captured;const received=new Promise(r=>captured=r);const barrier=new Promise(r=>release=r);let first=true;
    await a.route('**/api/connections',async route=>{
      if(!first){await route.continue();return;}first=false;
      const response=await route.fetch();captured();await barrier;await route.fulfill({response});delivered();
    });
    // A real successful reconnect broadcasts a refresh; A's old snapshot is held.
    await b.locator('[data-mcp-server="test"]').getByRole('button',{name:'重连',exact:true}).click();
    await received;
    await control(s,{token:'browser-new'});
    await b.getByRole('button',{name:'重新读取 .env'}).click();
    await expect(a.locator('[data-mcp-server="test"]')).toContainText('restart_required');
    release();await finished;
    await a.unroute('**/api/connections');
    await expect(a.locator('[data-mcp-server="test"]')).toContainText('restart_required');
    await a.goto(origin+'/tools');
    const tool=a.locator('article').filter({has:a.getByRole('heading',{name:'mcp_test_echo',exact:true})});
    await expect(tool).toContainText('模型暴露：hidden');
    expect((await control(s)).requests.filter(r=>r.method==='tools/call')).toHaveLength(0);
  }finally{release?.();await context.close();await s.close();}
});

test('MCP CE-09 CE-15 real pending HTTP replay retains one process and rejects competing operation',async({browser})=>{
  const s=await server();const context=await browser.newContext();let held=false;
  try{
    const page=await context.newPage();const origin=`http://127.0.0.1:${s.entry.port}`;
    await page.goto(origin+'/connections');
    const entry=await (await fetch(origin+'/api/entry')).json();
    const snapshot=await (await fetch(origin+'/api/connections')).json();
    const body={action:'reconnect',server_key:'test',token:snapshot.mcp.token,operation_id:'held-initialize'};
    const post=async payload=>{const r=await fetch(origin+'/api/connections/mcp',{method:'POST',headers:{'content-type':'application/json','x-agent-alfred-csrf':entry.csrf_token,'origin':origin},body:JSON.stringify(payload)});return {status:r.status,body:await r.json()};};
    await control(s,{hold_initialize:true});held=true;
    expect((await post(body)).status).toBe(202);
    await expect.poll(async()=>(await control(s)).requests.filter(r=>r.method==='initialize').length).toBe(2);
    expect((await post(body)).body.status).toBe('running');
    expect((await post({...body,operation_id:'competing'})).body.code).toBe('mutation_in_flight');
    expect((await post({...body,action:'cleanup'})).body.code).toBe('operation_conflict');
    await control(s,{release_initialize:true});held=false;
    await expect.poll(async()=>(await (await fetch(origin+'/api/connections/mcp/operation?operation_id=held-initialize')).json()).status).toBe('completed');
    expect((await post(body)).body.status).toBe('completed');
    expect((await control(s)).requests.filter(r=>r.method==='initialize')).toHaveLength(2);
  }finally{if(held)await control(s,{release_initialize:true}).catch(()=>{});await context.close();await s.close();}
});

test('MCP CE-14 delayed Tools response cannot undo new environment isolation',async({browser})=>{
  const s=await server();const context=await browser.newContext();let release;
  try{
    const a=await context.newPage(),b=await context.newPage();const origin=`http://127.0.0.1:${s.entry.port}`;
    await a.goto(origin+'/tools');await b.goto(origin+'/connections');
    const tool=a.locator('article').filter({has:a.getByRole('heading',{name:'mcp_test_echo',exact:true})});
    await tool.getByRole('combobox').selectOption('allowed');await tool.getByRole('button',{name:'保存授权',exact:true}).click();
    await expect(tool).toContainText('模型暴露：real');
    let captured,delivered;const received=new Promise(r=>captured=r),finished=new Promise(r=>delivered=r);
    const barrier=new Promise(r=>release=r);let first=true;
    await a.route('**/api/tools',async route=>{
      if(!first){await route.continue();return;}first=false;
      const response=await route.fetch();captured();await barrier;await route.fulfill({response});delivered();
    });
    await a.getByRole('button',{name:'核对当前授权',exact:true}).click();await received;
    await control(s,{token:'browser-new'});await b.getByRole('button',{name:'重新读取 .env'}).click();
    await expect(b.locator('[data-mcp-server="test"]')).toContainText('restart_required');
    await a.getByRole('button',{name:'核对当前授权',exact:true}).click();
    await expect(tool).toContainText('模型暴露：hidden');
    release();await finished;
    await expect(tool).toContainText('模型暴露：hidden');
    expect((await control(s)).requests.filter(r=>r.method==='tools/call')).toHaveLength(0);
  }finally{release?.();await context.close();await s.close();}
});

test('S08 lost MCP receipt requires explicit same-operation retry and preserves connection details',async({browser},testInfo)=>{
  const s=await server(),context=await browser.newContext({reducedMotion:'reduce'});
  try {
    const page=await context.newPage(),origin=`http://127.0.0.1:${s.entry.port}`;
    await page.goto(origin+'/connections');
    const card=page.locator('[data-mcp-server="test"]');
    await card.getByText('服务器详情',{exact:true}).click();
    const before=(await control(s)).requests.filter(r=>r.method==='initialize').length;
    const payloads=[];
    await page.route('**/api/connections/mcp',async route=>{payloads.push(route.request().postDataJSON());const response=await route.fetch();if(payloads.length===1)await route.abort();else await route.fulfill({response});});
    await card.getByRole('button',{name:'重连',exact:true}).click();
    await expect(page.getByRole('button',{name:'重试同一 MCP 操作',exact:true})).toBeVisible();
    await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
    await expect(card.locator('details')).toHaveAttribute('open','');
    expect(payloads).toHaveLength(1);
    await page.getByRole('button',{name:'重试同一 MCP 操作',exact:true}).click();
    await expect(page.getByText(/MCP 操作 completed/)).toBeVisible();
    expect(payloads).toHaveLength(2);expect(payloads[1]).toEqual(payloads[0]);
    expect((await control(s)).requests.filter(r=>r.method==='initialize')).toHaveLength(before+1);
    // The receipt notice precedes the final current-state read and its card redraw.
    await expect(page.getByText(`最近操作 ${payloads[0].operation_id}：completed`,{exact:false})).toBeVisible();
    await expect(card.getByRole('button',{name:'重连',exact:true})).toBeEnabled();
    await expect(card.locator('details')).toHaveAttribute('open','');
    const dimensions=[];
    for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
      await page.setViewportSize({width,height});await card.scrollIntoViewIfNeeded();
      await card.getByRole('button',{name:'重连',exact:true}).focus();await expect(card.getByRole('button',{name:'重连',exact:true})).toBeFocused();
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
      dimensions.push({viewport:[width,height],central:await page.locator('#page').boundingBox()});
      await page.screenshot({path:testInfo.outputPath(`connections-${width}.png`)});
    }
    console.log('S08 Connections central widths',JSON.stringify(dimensions));
    await testInfo.attach('central-dimensions',{body:JSON.stringify(dimensions,null,2),contentType:'application/json'});
  }finally{await context.close();await s.close();}
});
