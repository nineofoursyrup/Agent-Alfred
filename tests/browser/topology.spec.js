import {test, expect} from '@playwright/test';
import {memoryServer} from './memory-server.js';
import {observeTopology} from './topology-observation.js';

test('CE-01/02/05/08: real graphs, complete details and independent business forms', async ({page}) => {
  const server = await memoryServer({script:'tests/browser/routing_server.py'});
  const writes = [];
  page.on('request', r => {if (r.method() === 'POST') writes.push(r.url());});
  try {
    await page.goto(server.origin + '/behaviour');
    const routing = page.getByRole('region', {name:'消息分流', exact:true});
    const aggregation = page.getByRole('region', {name:'手动聚合', exact:true});
    await expect(routing.getByRole('button', {name:'查看流程', exact:true})).toHaveAttribute('aria-expanded', 'false');
    await page.getByRole('checkbox', {name:'启用消息分流'}).check();
    await page.getByRole('textbox', {name:'聚合目标'}).fill('未提交的目标');
    await page.getByRole('checkbox', {name:'语义记忆', exact:true}).uncheck();
    for (const [region, workflow] of [[routing,'message_routing'], [aggregation,'manual_aggregation']]) {
      await region.getByRole('button', {name:'查看流程', exact:true}).click();
      await expect(region.getByText('读取时的结构', {exact:false}).first()).toBeVisible();
      const snapshot = await (await page.request.get(server.origin + '/api/behaviour/topology?workflow=' + workflow)).json();
      const {nodes, edges} = snapshot.description.topology;
      expect(await region.locator('svg [data-node-id]').evaluateAll(els => els.map(el=>el.getAttribute('data-node-id')).sort())).toEqual(nodes.map(n=>n.node_id).sort());
      expect(await region.locator('svg [data-graph-edge]').evaluateAll(els => els.map(el=>JSON.parse(el.getAttribute('data-graph-edge'))))).toEqual(edges);
      expect(await region.locator('[data-topology-edge]').evaluateAll(els => els.map(el=>JSON.parse(el.getAttribute('data-topology-edge'))))).toEqual(edges);
      for (const n of nodes) await expect(region.getByRole('button', {name:snapshot.description.presentation.nodes[n.node_id].name + ' · ' + n.node_id, exact:true})).toBeVisible();
      await region.getByRole('button', {name:'放大', exact:true}).click();
      await region.getByRole('button', {name:'重新读取', exact:true}).click();
      await expect(region.getByText('重新读取中', {exact:false})).toHaveCount(0);
      await region.getByRole('button', {name:'收起流程', exact:true}).click();
    }
    await expect(page.getByRole('checkbox', {name:'启用消息分流'})).toBeChecked();
    await expect(page.getByRole('textbox', {name:'聚合目标'})).toHaveValue('未提交的目标');
    await expect(page.getByRole('textbox', {name:'聚合关键词'})).toHaveValue('未提交的目标');
    await expect(page.getByRole('checkbox', {name:'语义记忆', exact:true})).not.toBeChecked();
    expect(writes).toEqual([]);
    expect((await (await page.request.get(server.origin+'/api/behaviour')).json()).enabled).toBe(false);
    expect((await (await page.request.get(server.origin+'/api/runs')).json()).runs).toEqual([]);
    await page.reload();
    await expect(page.getByRole('button', {name:'查看流程', exact:true})).toHaveCount(2);
    await expect(page.getByRole('checkbox', {name:'启用消息分流'})).not.toBeChecked();
  } finally {await server.close();}
});

const routePath='**/api/behaviour/topology?workflow=message_routing';
const section=page=>page.getByRole('region',{name:'消息分流',exact:true});
async function openGraph(page,server) {
  await page.goto(server.origin+'/behaviour');
  // Wait for the real connection identity before capturing a response.
  await expect(page.getByRole('button',{name:'新建会话',exact:true})).toBeEnabled();
  const region=section(page);
  await region.getByRole('button',{name:'查看流程',exact:true}).click();
  await expect(region.locator('svg [data-node-id]')).toHaveCount(9);
  return region;
}
async function identity(region) {
  const detail=region.getByText('快照身份与输入声明',{exact:true});
  const parent=detail.locator('..');
  if ((await parent.getAttribute('open')) === null) await detail.click();
  return JSON.parse(await parent.locator('pre').textContent());
}
function barrier() {let release;const promise=new Promise(r=>release=r);return {promise,release};}
// Keep interception enabled until held responses finish. Removing the last
// route with times:1 can release Chrome's paused request before fulfilment.
async function interceptOnce(page,pattern,handler) {
  let used=false;
  await page.route(pattern,async route=>{
    if(used) return route.continue();
    used=true;await handler(route);
  });
}

for (const fault of ['schema','duplicate','dangling','terminal','missing']) {
  test(`CE-04: explicit protocol injection ${fault} preserves old graph then recovers`,async({page})=>{
    const server=await memoryServer({script:'tests/browser/topology_server.py'});
    try {
      const region=await openGraph(page,server);
      const old=await identity(region);
      await page.route(routePath,async route=>{
        const response=await route.fetch();const body=await response.json();
        if (fault==='schema') body.description.schema_version=999;
        if (fault==='duplicate') body.description.topology.nodes.push(body.description.topology.nodes[0]);
        if (fault==='dangling') body.description.topology.edges[0].target='does_not_exist';
        if (fault==='terminal') body.description.topology.nodes[0].terminal={kind:'imaginary'};
        if (fault==='missing') delete body.description.topology.routers;
        await route.fulfill({response,json:body});
      });
      await region.getByRole('button',{name:'重新读取',exact:true}).click();
      await expect(region.getByText(/上次成功的旧快照；当前响应无法展示/)).toBeVisible();
      expect(await identity(region)).toEqual(old);
      await expect(region.locator('svg [data-node-id]')).toHaveCount(9);
      await expect(page.getByRole('checkbox',{name:'启用消息分流'})).toBeEnabled();
      await page.unroute(routePath);
      await region.getByRole('button',{name:'重新读取',exact:true}).click();
      await expect(region.getByText(/当前响应无法展示/)).toHaveCount(0);
      await expect(region.locator('svg [data-node-id]')).toHaveCount(9);
    } finally {await server.close();}
  });
}

test('CE-03/05/08: actual publication, reverse HTTP responses, unknown text, selection and viewport',async({page})=>{
  const server=await memoryServer({script:'tests/browser/topology_server.py'});
  const captured=barrier(),release=barrier();
  try {
    const region=await openGraph(page,server);
    await region.getByRole('button',{name:'判断消息类型 · classify',exact:true}).click();
    await region.getByRole('button',{name:'放大',exact:true}).click();
    const viewport=await region.locator('svg').getAttribute('viewBox');
    await region.getByRole('button',{name:'收起流程',exact:true}).click();
    await region.getByRole('button',{name:'查看流程',exact:true}).click();
    await expect(region.getByText(/重新读取中/)).toHaveCount(0);
    expect(await region.locator('svg').getAttribute('viewBox')).toBe(viewport);
    await expect(region.getByRole('heading',{name:'判断消息类型 · classify'})).toBeVisible();
    const first=await identity(region);
    await server.send('publish-baseline');
    // Publishing never replaces an open reader or polls for another graph.
    expect(await identity(region)).toEqual(first);
    await region.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region.getByText(/重新读取中/)).toHaveCount(0);
    expect((await identity(region)).publication_generation).toBeGreaterThan(first.publication_generation);
    expect(await region.locator('svg').getAttribute('viewBox')).toBe(viewport);
    await expect(region.getByRole('heading',{name:'判断消息类型 · classify'})).toBeVisible();
    await interceptOnce(page,routePath,async route=>{
      const response=await route.fetch();captured.release();await release.promise;
      await route.fulfill({response}).catch(()=>{});
    });
    await region.getByRole('button',{name:'重新读取',exact:true}).click();await captured.promise;
    await expect(region.getByText(/旧快照 · 重新读取中/)).toBeVisible();
    await server.send('publish-alternate');
    await region.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region.locator('svg [data-node-id]')).toHaveCount(2);
    const latest=await identity(region);
    expect(latest.publication_generation).toBeGreaterThan(first.publication_generation);
    await expect(region.getByText(/结构已更新/)).toBeVisible();
    await expect(region.getByRole('heading',{name:'判断消息类型 · classify'})).toHaveCount(0);
    expect(await region.locator('svg').getAttribute('viewBox')).not.toBe(viewport);
    await expect(region.getByRole('button',{name:'future_step · future_step'})).toBeVisible();
    await expect(region.locator("li").filter({hasText:/future_branch：暂无说明/})).toBeVisible();
    release.release(); await page.unrouteAll({behavior:'wait'});
    expect(await identity(region)).toEqual(latest);
    await server.send('counts');
    const {readFile}=await import('node:fs/promises');
    expect(JSON.parse(await readFile(server.directory+'/counts.json','utf8')).model_requests).toBe(0);
  } finally {release.release();await server.close();}
});

test('CE-03/04: transport failure, initial retry, independent graphs and real startup absence',async({page})=>{
  const server=await memoryServer({script:'tests/browser/topology_server.py'});
  try {
    await page.goto(server.origin+'/behaviour');
    const region=section(page);
    await page.route(routePath,route=>route.abort());
    await region.getByRole('button',{name:'查看流程',exact:true}).click();
    await expect(region.getByText('首次读取失败，请重试读取。')).toBeVisible();
    await page.unroute(routePath);
    await region.getByRole('button',{name:'重试读取',exact:true}).click();
    await expect(region.locator('svg [data-node-id]')).toHaveCount(9);
    const old=await identity(region);
    await server.send('publish-alternate');
    await page.route(routePath,route=>route.fulfill({status:503,json:{code:'topology_read_failed'}}));
    await region.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region.getByText(/旧快照 \/ 本次读取失败/)).toBeVisible();
    await expect(region.locator('svg [data-node-id]')).toHaveCount(9);
    expect(await identity(region)).toEqual(old);
    const agg=page.getByRole('region',{name:'手动聚合',exact:true});
    await agg.getByRole('button',{name:'查看流程',exact:true}).click();
    await expect(agg.locator('svg [data-node-id]')).toHaveCount(15);
    await page.unroute(routePath);
    const {writeFile}=await import('node:fs/promises');
    await writeFile(server.directory+'/graph-mode','missing');
    await server.restart();
    await expect(region.getByText(/来自上一服务实例/)).toBeVisible();
    await region.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region.getByText(/当前没有已发布的图/)).toBeVisible();
    await expect(region.locator('svg')).toHaveCount(0);
    await server.send('publish-baseline');
    await region.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region.locator('svg [data-node-id]')).toHaveCount(9);
  } finally {await server.close();}
});

for (const action of ['collapse','navigate','restart']) {
  test(`CE-06: ${action} invalidates captured real P1 response`,async({page})=>{
    const server=await memoryServer({script:'tests/browser/topology_server.py'});
    const captured=barrier(),release=barrier();
    try {
      let region=await openGraph(page,server);const first=await identity(region);
      await page.getByRole('textbox',{name:'聚合目标'}).fill('保留草稿');
      await interceptOnce(page,routePath,async route=>{const response=await route.fetch();captured.release();await release.promise;await route.fulfill({response}).catch(()=>{});});
      await region.getByRole('button',{name:'重新读取',exact:true}).click();await captured.promise;
      if(action==='collapse') {
        await region.getByRole('button',{name:'收起流程',exact:true}).click();
        await server.send('publish-alternate');
        await region.getByRole('button',{name:'查看流程',exact:true}).click();
        await expect(region.locator('svg [data-node-id]')).toHaveCount(2);
      } else if(action==='navigate') {
        await page.getByRole('link',{name:'运行',exact:true}).click();
        release.release();await page.unrouteAll({behavior:'wait'});
        await expect(page.getByRole('region',{name:'流程拓扑'})).toHaveCount(0);
        await page.getByRole('link',{name:'Behaviour',exact:true}).click();
        region=section(page);
        await expect(region.getByRole('button',{name:'查看流程'})).toHaveAttribute('aria-expanded','false');
        return;
      } else {
        await server.restart();
        await expect(region.getByText(/来自上一服务实例/)).toBeVisible();
        await region.getByRole('button',{name:'重新读取',exact:true}).click();
        await expect(region.getByText(/来自上一服务实例/)).toHaveCount(0);
      }
      const latest=await identity(region);
      if(action==='restart') {expect(latest.process_instance_id).not.toBe(first.process_instance_id);expect(latest.topology_hash).toBe(first.topology_hash);}
      release.release();await page.unrouteAll({behavior:'wait'});
      expect(await identity(region)).toEqual(latest);
      await expect(page.getByRole('textbox',{name:'聚合目标'})).toHaveValue('保留草稿');
    } finally {release.release();await server.close();}
  });
}

for (const width of [1280,390]) {
  test(`CE-08: keyboard and complete text at ${width}px`,async({page})=>{
    const server=await memoryServer({script:'tests/browser/topology_server.py'});
    try {
      await page.setViewportSize({width,height:850});
      await page.goto(server.origin+'/behaviour');
      for (const name of ['消息分流','手动聚合']) {
        const region=page.getByRole('region',{name,exact:true});
        const open=region.getByRole('button',{name:'查看流程',exact:true});
        await open.focus();await page.keyboard.press('Enter');
        const count=name==='消息分流'?9:15;
        await expect(region.locator('svg [data-node-id]')).toHaveCount(count);
        await region.locator('svg').screenshot({path:`output/playwright/topology-canvas-${name==='消息分流'?'routing':'aggregation'}-${width}.png`});
        // Tab from the toggle reaches refresh and every viewport control.
        await page.keyboard.press('Tab');
        await expect(region.getByRole('button',{name:'重新读取',exact:true})).toBeFocused();
        await page.keyboard.press('Enter');await expect(region.getByText(/重新读取中/)).toHaveCount(0);
        await page.keyboard.press('Tab');await expect(region.getByRole('button',{name:'放大',exact:true})).toBeFocused();
        const initial=await region.locator('svg').getAttribute('viewBox');await page.keyboard.press('Enter');
        expect(await region.locator('svg').getAttribute('viewBox')).not.toBe(initial);
        await page.keyboard.press('Tab');await page.keyboard.press('Tab');
        await expect(region.getByRole('button',{name:'适应全图',exact:true})).toBeFocused();await page.keyboard.press('Enter');
        expect(await region.locator('svg').getAttribute('viewBox')).toBe(initial);
        // Every node is selectable in text, without interacting with the SVG.
        const buttons=region.locator('li button');
        for(let i=0;i<await buttons.count();i++) {
          const b=buttons.nth(i);await b.focus();await page.keyboard.press('Enter');
          await expect(region.getByRole('heading',{name:await b.textContent(),exact:true})).toBeVisible();
        }
        const edges=region.locator('[data-topology-edge]');
        await expect(edges).toHaveCount(name==='消息分流'?10:20);
        for(const item of await edges.all()) {await item.scrollIntoViewIfNeeded();await expect(item).toBeVisible();}
        const overflow=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,elements:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>({tag:e.tagName,cls:e.className,text:e.textContent?.slice(0,70),right:e.getBoundingClientRect().right})).slice(0,12)}));
        expect(overflow.scroll,JSON.stringify(overflow)).toBeLessThanOrEqual(overflow.width);
        await page.screenshot({path:`output/playwright/topology-${name==='消息分流'?'routing':'aggregation'}-${width}.png`,fullPage:true});
        const close=region.getByRole('button',{name:'收起流程',exact:true});await close.focus();await page.keyboard.press('Enter');
        await expect(region.locator('svg')).toBeHidden();
      }
    } finally {await server.close();}
  });
}

test('CE-01/03: disconnection and recovery never replace a snapshot or save drafts',async({page})=>{
  const server=await memoryServer({script:'tests/browser/topology_server.py'});
  try {
    let reads=0;
    page.on('request',r=>{if(r.url().includes('/api/behaviour/topology')) reads++;});
    const region=await openGraph(page,server);const first=await identity(region);
    await page.getByRole('checkbox',{name:'启用消息分流'}).check();
    await page.evaluate(()=>window.dispatchEvent(new Event('offline')));
    await expect(region.getByText(/服务连接已断开/)).toBeVisible();
    await server.send('publish-alternate');
    await page.evaluate(()=>window.dispatchEvent(new Event('online')));
    await expect(region.getByText(/服务已连接/)).toBeVisible();
    expect(await identity(region)).toEqual(first);expect(reads).toBe(1);
    await expect(page.getByRole('checkbox',{name:'启用消息分流'})).toBeChecked();
    await region.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region.locator('svg [data-node-id]')).toHaveCount(2);expect(reads).toBe(2);
  } finally {await server.close();}
});

test('AC-14: protocol text injection is shown literally and never becomes markup',async({page})=>{
  const server=await memoryServer({script:'tests/browser/topology_server.py'});
  try {
    const region=await openGraph(page,server);
    const literal='<img src=x onerror="window.topologyInjected=true">';
    await page.route(routePath,async route=>{
      const response=await route.fetch(),body=await response.json();
      body.description.presentation.nodes.classify.description=literal;
      body.description.presentation.branches.join_route.quick=literal;
      await route.fulfill({response,json:body});
    });
    await region.getByRole('button',{name:'重新读取',exact:true}).click();
    await expect(region.getByText(literal,{exact:true})).toBeVisible();
    expect(await region.locator('img').count()).toBe(0);
    expect(await page.evaluate(()=>window.topologyInjected)).toBeUndefined();
  } finally {await server.close();}
});

test('CE-06: first connection identity does not erase an already open first read',async({page})=>{
  const server=await memoryServer({script:'tests/browser/topology_server.py'});
  const entry=barrier(), graph=barrier(), captured=barrier();
  try {
    await interceptOnce(page,'**/api/entry',async route=>{const response=await route.fetch();await entry.promise;await route.fulfill({response});});
    await interceptOnce(page,routePath,async route=>{const response=await route.fetch();captured.release();await graph.promise;await route.fulfill({response}).catch(()=>{});});
    await page.goto(server.origin+'/behaviour',{waitUntil:'commit'});
    const region=section(page);
    await region.getByRole('button',{name:'查看流程',exact:true}).click();await captured.promise;
    entry.release();await expect(region.getByText(/服务已连接/)).toBeVisible();
    await expect(region.getByText('正在读取…',{exact:true})).toBeVisible();
    graph.release();await expect(region.locator('svg [data-node-id]')).toHaveCount(9);
  } finally {entry.release();graph.release();await server.close();}
});


test('CE-01 / AC-12: real settings save with lost response stays unconfirmed during observation',async({page})=>{
  const server=await memoryServer({script:'tests/browser/topology_server.py'});
  try {
    await page.goto(server.origin+'/behaviour');
    await expect(page.getByRole('button',{name:'新建会话',exact:true})).toBeEnabled();
    await page.getByRole('checkbox',{name:'启用消息分流'}).check();
    let saved=0;
    await page.route('**/api/behaviour',async route=>{
      if(route.request().method()!=='POST') return route.continue();
      const response=await route.fetch();expect(response.status()).toBe(200);saved++;
      await route.abort();
    });
    await page.getByRole('button',{name:'保存设置',exact:true}).click();
    await expect(page.getByText('保存结果未确认，请刷新核验。')).toBeVisible();
    await observeTopology(page);
    await expect(page.getByText('保存结果未确认，请刷新核验。')).toBeVisible();
    await expect(page.getByRole('checkbox',{name:'启用消息分流'})).toBeChecked();
    expect(saved).toBe(1);
    expect((await (await page.request.get(server.origin+'/api/behaviour')).json()).enabled).toBe(true);
  } finally {await server.close();}
});


for (const [name,workflow,count] of [['消息分流','message_routing',9],['手动聚合','manual_aggregation',15]]) {
  test(`STD-01/SPEC-01: real mouse selection and drag in ${workflow}`,async({page})=>{
    const server=await memoryServer({script:'tests/browser/topology_server.py'});
    try {
      await page.setViewportSize({width:1280,height:1000});
      await page.goto(server.origin+'/behaviour');
      const region=page.getByRole('region',{name,exact:true});
      await region.getByRole('button',{name:'查看流程',exact:true}).click();
      const svg=region.locator('svg');
      await expect(svg.locator('[data-node-id]')).toHaveCount(count);
      const snapshot=await (await page.request.get(server.origin+'/api/behaviour/topology?workflow='+workflow)).json();
      const [first,second]=snapshot.description.topology.nodes;
      const label=n=>snapshot.description.presentation.nodes[n.node_id].name+' · '+n.node_id;
      // Locator.click sends a real mouse down/up sequence, including capture.
      await svg.locator(`[data-node-id="${first.node_id}"]`).click();
      await expect(region.getByRole('heading',{name:label(first),exact:true})).toBeVisible();
      await svg.locator(`[data-node-id="${second.node_id}"]`).click();
      await expect(region.getByRole('heading',{name:label(second),exact:true})).toBeVisible();
      await expect(region.getByRole('heading',{name:label(first),exact:true})).toHaveCount(0);
      // Drag starting on another node pans without selecting that node.
      const target=svg.locator(`[data-node-id="${first.node_id}"]`);
      await target.scrollIntoViewIfNeeded();
      const box=await target.boundingBox();
      const before=await svg.getAttribute('viewBox');
      await page.mouse.move(box.x+box.width/2,box.y+box.height/2);
      await page.mouse.down();
      await page.mouse.move(box.x+box.width/2+40,box.y+box.height/2+30,{steps:5});
      await page.mouse.up();
      expect(await svg.getAttribute('viewBox')).not.toBe(before);
      await expect(region.getByRole('heading',{name:label(second),exact:true})).toBeVisible();
      const moved=await svg.getAttribute('viewBox');
      await region.getByRole('button',{name:'重新读取',exact:true}).click();
      await expect(region.getByText(/重新读取中/)).toHaveCount(0);
      expect(await svg.getAttribute('viewBox')).toBe(moved);
      await expect(region.getByRole('heading',{name:label(second),exact:true})).toBeVisible();
      await region.getByRole('button',{name:label(first),exact:true}).focus();
      await page.keyboard.press('Enter');
      await expect(region.getByRole('heading',{name:label(first),exact:true})).toBeVisible();
    } finally {await server.close();}
  });
}
