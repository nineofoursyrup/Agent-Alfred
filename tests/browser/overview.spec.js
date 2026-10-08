import {test, expect} from '@playwright/test';
import {localServer} from './local-server.js';
import {memoryServer} from './memory-server.js';
import {emit} from './transport.js';

const fixture=()=>memoryServer({script:'tests/browser/overview_server.py'});

test('Overview reads three real independent sources without selecting a Session', async ({page}) => {
  const server = await localServer();
  const writes = [], reads = [];
  page.on('request', request => {
    if (request.method() === 'POST') writes.push(request.url());
    if (request.url().includes('/api/overview/')) reads.push(request.url());
  });
  try {
    const response = await page.goto(server.origin + '/overview');
    expect(response.status()).toBe(200);
    await expect(page.getByRole('heading', {name:'总览', exact:true})).toBeVisible();
    await expect(page.getByLabel('期间指标')).toContainText('暂无运行记录');
    await expect(page.getByLabel('当前记忆条目数')).toContainText('尚无已保存的语义／情景记忆');
    await expect(page.getByLabel('全历史最近运行')).toContainText('暂无可展示的已结束运行');
    await expect(page.getByRole('heading', {name:'静态架构说明',exact:true})).toBeVisible();
    expect(await page.evaluate(() => sessionStorage.getItem('alfred.session'))).toBeNull();
    expect(writes).toEqual([]);
    expect(reads).toHaveLength(3);
    expect(new URL(reads.find(url=>url.includes('/period?'))).searchParams.get('range')).toBe('7d');
    await page.getByLabel('总览期间').selectOption('today');
    await expect.poll(()=>reads.length).toBe(4);
    expect(reads.filter(url=>url.includes('/memory-counts?'))).toHaveLength(1);
    expect(reads.filter(url=>url.includes('/recent-runs?'))).toHaveLength(1);
  } finally { await server.close(); }
});

test('Overview preserves full counts, separate costs and fixed calendar source on navigation', async ({page}) => {
  test.setTimeout(60000);
  const server=await fixture();
  try {
    await server.send('time 2026-03-08T16:00:00+00:00');
    await server.send('mode mixed');await server.send('runs 1');await server.send('mode exact');await server.send('runs 55');
    await server.send('memory 101');
    await page.goto(server.origin+'/overview?range=today&timezone=America%2FNew_York');
    const period=page.getByLabel('期间指标');
    await expect(period).toContainText('56 条 Run 记录');
    await expect(period).toContainText('精确 USD 8.125');
    await expect(period).toContainText('估算 USD 0.003');
    await expect(period).toContainText('费用未知 1 次');
    await expect(period).toContainText('catalog');
    await expect(period).toContainText('过期价格');
    await expect(page.getByLabel('当前记忆条目数')).toContainText('语义 101 条');
    await expect(page.locator('.overview-run')).toHaveCount(5);
    await expect(page.locator('#page')).not.toContainText('synthetic overview fixture');
    await period.getByText('本次期间与来源',{exact:true}).click();
    await expect(period).toContainText('[2026-03-08, 2026-03-09)');
    await expect(period).toContainText('2026-03-08T05:00:00+00:00');
    const href=await page.getByRole('link',{name:'查看本期账目（按原期间重新读取）'}).getAttribute('href');
    const params=new URL(href,server.origin).searchParams;
    expect(Object.fromEntries(params)).toEqual({range:'custom',timezone:'America/New_York',start:'2026-03-08',end:'2026-03-09'});
    await server.send('advance-day');await server.send('price-change');
    await expect(period.locator('.overview-window')).toBeVisible();
    await expect(period.locator('.overview-window')).toContainText('[2026-03-08, 2026-03-09)');
    await page.getByRole('link',{name:'查看本期账目（按原期间重新读取）'}).click();
    await expect(page.getByLabel('账目汇总')).toContainText('0.03');
    await expect(page.getByLabel('开始日期（含）')).toHaveValue('2026-03-08');
    await expect(page.getByLabel('结束日期（不含）')).toHaveValue('2026-03-09');
  } finally {await server.close();}
});

test('Unknown, proven zero, no consumption, tiny positive and damaged membership stay distinct', async ({page}) => {
  const server=await fixture();
  try {
    await server.send('mode unknown');await server.send('runs 1');
    await page.goto(server.origin+'/overview');
    const period=page.getByLabel('期间指标');
    await expect(period).toContainText('费用未知');
    await expect(period).not.toContainText('该范围零费用');
    await server.send('telemetry no-model');
    await page.getByRole('button',{name:'刷新期间指标',exact:true}).click();
    await expect(period).toContainText('该范围无模型消耗');
    await server.send('mode zero');await server.send('runs 1');
    await page.getByRole('button',{name:'刷新期间指标',exact:true}).click();
    await expect(period).toContainText('该范围零费用');
    await server.send('mode tiny');await server.send('runs 1');
    await page.getByRole('button',{name:'刷新期间指标',exact:true}).click();
    await expect(period).toContainText('精确 USD <0.0001');
    await period.getByText('本次期间与来源',{exact:true}).click();
    await expect(period).toContainText('0.000000001');
    await server.send('telemetry legacy-tool-gap');
    await page.getByRole('button',{name:'刷新期间指标',exact:true}).click();
    await expect(period).toContainText('其他账目覆盖不足');
    await expect(period).toContainText('模型账目完整');
    await server.send('telemetry bad-time');
    await page.getByRole('button',{name:'刷新期间指标',exact:true}).click();
    await expect(period).toContainText('已定位 0 条；另 3 条期间无法判定');
    await expect(period).not.toContainText('暂无运行记录');
    await expect(period).not.toContainText('该范围零费用');
    await server.send('telemetry interrupted');
    await page.getByRole('button',{name:'刷新最近运行'}).click();
    await expect(page.getByLabel('全历史最近运行')).toContainText('终态无法确认');
    await expect(page.getByLabel('全历史最近运行')).toContainText('记录状态未知');
    await expect(page.getByLabel('全历史最近运行')).not.toContainText('已保存');
  } finally {await server.close();}
});

test('Three sources fail independently, retire replaced ranges and allow local retries', async ({page}) => {
  const server=await fixture();
  let release;const gate=new Promise(resolve=>{release=resolve;});
  try {
    await server.send('runs 1');await server.send('memory 1');
    await page.route('**/api/overview/memory-counts?**',route=>route.abort());
    await page.goto(server.origin+'/overview');
    await expect(page.getByLabel('期间指标')).toContainText('1 条 Run 记录');
    await expect(page.getByLabel('当前记忆条目数')).toContainText('读取失败');
    await expect(page.locator('.overview-run')).toHaveCount(1);
    await page.unroute('**/api/overview/memory-counts?**');
    await page.getByRole('button',{name:'刷新记忆计数'}).click();
    await expect(page.getByLabel('当前记忆条目数')).toContainText('语义 1 条');
    await page.route('**/api/overview/period?**',async route=>{const response=await route.fetch();await gate;await route.fulfill({response}).catch(()=>{});});
    const requested=page.waitForRequest('**/api/overview/period?**');
    await page.getByRole('button',{name:'刷新期间指标'}).click();await requested;
    await page.getByLabel('总览期间').selectOption('today');
    await expect(page.getByLabel('期间指标')).not.toContainText('1 条 Run 记录');
    await expect(page.getByLabel('当前记忆条目数')).toContainText('语义 1 条');
    await page.unroute('**/api/overview/period?**');
    await page.getByLabel('总览期间').selectOption('30d');
    await expect(page.getByLabel('期间指标')).toContainText('1 条 Run 记录');
    const now=await page.getByLabel('期间指标').textContent();release();
    await expect(page.getByLabel('总览期间')).toHaveValue('30d');
    await expect(page.getByLabel('期间指标')).toHaveText(now);
  } finally {release();await server.close();}
});

test('Hidden Overview revokes a changed memory revision and rejects its delayed response', async ({page}) => {
  const server=await fixture();
  let release;const gate=new Promise(resolve=>{release=resolve;});
  let received;const fetched=new Promise(resolve=>{received=resolve;});
  try {
    await server.send('memory 1');await page.setViewportSize({width:390,height:844});
    await page.goto(server.origin+'/overview');
    const memory=page.getByLabel('当前记忆条目数');
    await expect(memory).toContainText('语义 1 条');
    await page.route('**/api/overview/memory-counts?**',async route=>{const response=await route.fetch();received();await gate;await route.fulfill({response}).catch(()=>{});});
    await page.getByRole('button',{name:'刷新记忆计数'}).click();await fetched;
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
    await server.send('memory 1');
    await expect(memory).toContainText('记忆修订已变化');
    await expect(memory).not.toContainText('语义 1 条');
    release();await page.goBack();
    await expect(memory).not.toContainText('语义 1 条');
    await page.unroute('**/api/overview/memory-counts?**');
    await page.getByRole('button',{name:'刷新记忆计数'}).click();
    await expect(memory).toContainText('语义 2 条');
  } finally {release();await server.close();}
});

test('Disconnect, same-instance reconnect and restart cannot renew or resurrect observations', async ({page,context}) => {
  const server=await fixture();
  try {
    await server.send('runs 1');await page.goto(server.origin+'/overview');
    const period=page.getByLabel('期间指标');
    await expect(period).toContainText('1 条 Run 记录');
    const observation=await period.locator('[role=status]').textContent();
    let requests=0;page.on('request',r=>{if(r.url().includes('/api/overview/'))requests++;});
    await context.setOffline(true);
    await expect(period).toContainText('离线副本');
    await context.setOffline(false);
    await expect(period).toContainText('连接已恢复，旧快照待刷新');
    expect(requests).toBe(0);
    await expect(period).toContainText(observation.replace('来源观察：',''));
    await server.restart();
    await expect(period).toContainText('实例已变化，请刷新');
    await expect(period).not.toContainText('1 条 Run 记录');
    expect(requests).toBe(0);
    await page.getByRole('button',{name:'刷新全部来源'}).click();
    await expect(period).toContainText('1 条 Run 记录');
  } finally {await context.setOffline(false);await server.close();}
});

test('Original observation expires despite a failed refresh and layout changes; earlier source expiry wins', async ({page}) => {
  const server=await fixture();
  try {
    await server.send('runs 1');await page.clock.install();await page.goto(server.origin+'/overview');
    const period=page.getByLabel('期间指标');
    await expect(period).toContainText('1 条 Run 记录');
    const before=await period.locator('[role=status]').textContent();
    await page.clock.fastForward(14*60000);
    await page.route('**/api/overview/period?**',route=>route.abort());
    await page.getByRole('button',{name:'刷新期间指标'}).click();
    await expect(period).toContainText('读取失败');
    await page.setViewportSize({width:390,height:844});
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();await page.goBack();
    await page.clock.fastForward(61000);
    await expect(period).toContainText('已过期');
    await expect(period).toContainText(before.replace('来源观察：',''));
    await page.unroute('**/api/overview/period?**');
    await page.clock.setSystemTime(new Date());
    await server.send('short-ttl');
    await page.getByRole('button',{name:'刷新期间指标'}).click();
    await expect(period).not.toContainText('已过期');
    await page.clock.fastForward(2100);
    await expect(period).toContainText('已过期');
  } finally {await server.close();}
});

test('Current saving Run shares one slot; ordinary completion only prompts a manual history refresh', async ({page}) => {
  const server=await fixture();
  try {
    await server.send('runs 6');await page.goto(server.origin+'/overview');
    await expect(page.locator('.overview-run')).toHaveCount(5);
    const ids=await page.locator('.overview-run').evaluateAll(rows=>rows.map(row=>row.dataset.runId));
    const entry=await (await page.request.get(server.origin+'/api/entry')).json();
    const headers={'x-agent-alfred-csrf':entry.csrf_token};
    const created=await page.request.post(server.origin+'/api/sessions',{headers,data:{}});
    const session=await created.json();expect(created.status(),JSON.stringify(session)).toBe(201);
    await server.send('hold-recording');
    const sent=await page.request.post(server.origin+'/api/runs',{headers,data:{session_id:session.session_id,message:'held offline Run'}});
    const accepted=await sent.json();expect(sent.status(),JSON.stringify(accepted)).toBe(202);
    await server.send('wait-recording');
    const current=page.getByLabel('当前运行槽');
    await expect(current).toContainText('正在保存');
    await expect(current).toContainText('已完成');
    await expect(current.locator('.overview-run')).toHaveAttribute('data-run-id',accepted.run_id);
    expect(await page.locator('.overview-history .overview-run').evaluateAll(rows=>rows.map(row=>row.dataset.runId))).toEqual(ids);
    await server.send('release-recording');
    await expect(current.locator('.overview-run')).toHaveCount(0);
    await expect(page.getByLabel('全历史最近运行')).toContainText('有新运行，刷新查看');
    expect(await page.locator('.overview-history .overview-run').evaluateAll(rows=>rows.map(row=>row.dataset.runId))).toEqual(ids);
    await page.getByRole('button',{name:'刷新最近运行'}).click();
    await expect(page.locator('.overview-history .overview-run').first()).toHaveAttribute('data-run-id',accepted.run_id);
    await expect(page.locator('.overview-history .overview-run')).toHaveCount(5);
    expect(await page.evaluate(()=>sessionStorage.getItem('alfred.session'))).toBeNull();
  } finally {await server.send('release-recording');await server.close();}
});

test('Invalid range and IANA timezone expose correction without blocking other sources', async ({page}) => {
  const server=await fixture();
  try {
    await server.send('runs 1');
    await page.goto(server.origin+'/overview?range=custom&timezone=Not_A_Zone');
    const period=page.getByLabel('期间指标');
    await expect(period).toContainText('期间不可用');
    await expect(page.locator('.overview-run')).toHaveCount(1);
    await page.getByLabel('总览期间').selectOption('today');
    await expect(period).toContainText('有效的 IANA 时区');
    await expect(period.getByRole('link',{name:'查看本期账目（按原期间重新读取）'})).toHaveCount(0);
    await page.getByLabel('IANA 时区', {exact:true}).fill('Asia/Shanghai');
    await page.getByRole('button',{name:'应用时区'}).click();
    await expect(period).toContainText('1 条 Run 记录');
    expect(new URL(page.url()).searchParams.get('timezone')).toBe('Asia/Shanghai');
  } finally {await server.close();}
});

test('Repeated Overview reads preserve another Ops snapshot and trace pruning keeps accounting', async ({page}) => {
  const server=await fixture();
  try {
    await server.send('runs 2');await server.send('retain-ops');
    await page.goto(server.origin+'/overview');
    const period=page.getByLabel('期间指标');
    await expect(period).toContainText('2 条 Run 记录');
    await server.send('prune');
    for(let i=0;i<9;i++) {
      await page.getByRole('button',{name:'刷新全部来源'}).click();
      await expect(page.getByRole('button',{name:'刷新期间指标'})).toBeEnabled();
      await expect(period).toContainText('精确 USD 0.25');
      await server.send('check-ops');
    }
    await expect(page.locator('.overview-run')).toHaveCount(2);
    await expect(page.getByLabel('全历史最近运行')).toContainText('已保存');
  } finally {await server.close();}
});

test('Source navigation records only identity and restores Overview after a real reread', async ({page}) => {
  const server=await fixture();
  let release=()=>{};
  try {
    await server.send('runs 1');await page.goto(server.origin+'/overview?range=30d&timezone=Asia%2FShanghai');
    const row=page.locator('.overview-run').first();await expect(row).toBeVisible();
    const id=await row.getAttribute('data-run-id');
    await page.locator('#new-session').click();const input=page.locator('#message');await input.fill('总览往返草稿');
    let streams=0,periodReads=0;
    page.on('request',r=>{if(r.url().includes('/api/events'))streams++;if(r.url().includes('/api/overview/period?'))periodReads++;});
    await page.evaluate(()=>{window.overviewSources=[];const push=history.pushState.bind(history);history.pushState=(value,...args)=>{window.overviewSources.push(value.alfredShell?.source);return push(value,...args);};});
    await row.getByRole('link').click();await expect(page).toHaveURL(/\/runs\//);
    const source=await page.evaluate(()=>window.overviewSources.at(-1).returnSource);
    expect(source).toMatchObject({kind:'overview',anchor:id,range:'30d',timezone:'Asia/Shanghai'});
    expect(JSON.stringify(source)).not.toContain('总览往返草稿');
    await page.goBack();
    await expect(page.getByLabel('期间指标')).toContainText('1 条 Run 记录');
    await expect(page.locator('.overview-run').getByRole('link')).toBeFocused();
    await expect(input).toHaveValue('总览往返草稿');
    expect(periodReads).toBe(1);expect(streams).toBe(0);
    const architectureMemory=page.getByLabel('静态架构说明').getByRole('link',{name:'记忆',exact:true});
    await architectureMemory.click();await expect(page).toHaveURL(/\/memory(?:\?|$)/);
    expect(await page.evaluate(()=>window.overviewSources.at(-1).returnSource.section)).toBe('静态架构说明');
    await page.goBack();
    await expect(architectureMemory).toBeFocused();
    await expect(page.getByRole('link',{name:'查看当前记忆（重新读取）'})).not.toBeFocused();

    // A later real user intent retires the pending restore of a data-backed Run anchor.
    await row.getByRole('link').click();await expect(page).toHaveURL(/\/runs\//);
    let fetched;const received=new Promise(resolve=>fetched=resolve),gate=new Promise(resolve=>release=resolve);
    await page.route('**/api/overview/recent-runs?**',async route=>{const response=await route.fetch();fetched();await gate;await route.fulfill({response}).catch(()=>{});});
    await page.goBack();await received;
    await input.click();await input.fill('返回后继续输入');
    release();await expect(page.locator('.overview-run')).toHaveCount(1);
    await expect(input).toBeFocused();await expect(input).toHaveValue('返回后继续输入');expect(streams).toBe(0);
  } finally {release();await server.close();}
});

// Retain native EventSource and real Host events; only frame delivery is controlled.
async function observeStream(page) {
  await page.addInitScript(()=>{
    window.sources=[];
    const Native=window.EventSource;
    window.EventSource=class extends Native {
      constructor(url){super(url);window.sources.push(this);}
      addEventListener(kind,callback,options){
        super.addEventListener(kind,event=>{
          if(kind==='state_patch'){
            const body=JSON.parse(event.data);
            if(body.active_run?.recording_state==='pending')window.lastPending=body;
            if(window.holdIdle&&body.coordinator_state==='idle'){
              (window.heldIdle??=[]).push(()=>callback(event));return;
            }
          }
          callback(event);
        },options);
      }
    };
  });
}

test('Gap and disconnect retire slow real reads even after a same-instance reconnect', async ({page,context}) => {
  const server=await fixture();
  let release=()=>{};
  try {
    await observeStream(page);await server.send('runs 1');await page.goto(server.origin+'/overview');
    const period=page.getByLabel('期间指标');await expect(period).toContainText('1 条 Run 记录');
    for(const interruption of ['gap','disconnect']) {
      await server.send('runs 1');
      let fetched;const received=new Promise(resolve=>fetched=resolve),gate=new Promise(resolve=>release=resolve);
      await page.route('**/api/overview/period?**',async route=>{const response=await route.fetch();fetched();await gate;await route.fulfill({response}).catch(()=>{});});
      await page.getByRole('button',{name:'刷新期间指标'}).click();await received;
      if(interruption==='gap') {await emit(page,'transport_notice',{code:'deltas_dropped'});await expect(period).toContainText('事件有缺口');}
      else {await context.setOffline(true);await expect(period).toContainText('离线副本');await context.setOffline(false);await expect(period).toContainText('连接已恢复');}
      release();await page.unroute('**/api/overview/period?**');
      await expect(period).toContainText('1 条 Run 记录');
      await expect(period).not.toContainText('2 条 Run 记录');
      await expect(period).not.toContainText('3 条 Run 记录');
    }
    await page.getByRole('button',{name:'刷新期间指标'}).click();
    await expect(period).toContainText('3 条 Run 记录');
  } finally {release();await context.setOffline(false);await server.close();}
});

test('Overview keeps opaque colliding identities and reference layouts readable', async ({page},testInfo) => {
  const server=await fixture();
  try {
    await server.send('runs 2');await server.send('opaque-runs');
    await page.emulateMedia({reducedMotion:'reduce'});await page.goto(server.origin+'/overview');
    await expect(page.locator('.overview-run')).toHaveCount(2);
    const ids=await page.locator('.overview-run').evaluateAll(rows=>rows.map(row=>row.dataset.runId));
    expect(ids[0]).not.toBe(ids[1]);
    for(const row of await page.locator('.overview-run').all()) {
      const id=await row.getAttribute('data-run-id');
      await expect(row.getByRole('link')).toHaveText('Run '+id);
      await expect(row.getByRole('link')).toHaveAttribute('href','/runs/'+encodeURIComponent(id));
    }
    await expect(page.getByLabel('全历史最近运行')).toContainText('未知用途：future-purpose');
    for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800],[1099,900],[1100,900]]) {
      await page.setViewportSize({width,height});
      await expect(page.getByRole('heading',{name:'总览',exact:true})).toBeVisible();
      const geometry=await page.evaluate(()=>({body:document.body.scrollWidth,viewport:innerWidth,page:document.querySelector('#page').clientWidth,pageScroll:document.querySelector('#page').scrollWidth,animation:getComputedStyle(document.querySelector('.overview-diagram')).animationName}));
      expect(geometry.body).toBeLessThanOrEqual(geometry.viewport);expect(geometry.pageScroll).toBeLessThanOrEqual(geometry.page+1);expect(geometry.animation).toBe('none');
      await page.locator('#page').evaluate(node=>node.scrollTop=0);
      await page.screenshot({path:testInfo.outputPath(`overview-${width}.png`)});
      if([1440,1280,390,320].includes(width)){
        await page.getByRole('heading',{name:'静态架构说明',exact:true}).scrollIntoViewIfNeeded();await page.screenshot({path:testInfo.outputPath(`architecture-${width}.png`)});
        await page.getByRole('heading',{name:'全历史最近运行',exact:true}).scrollIntoViewIfNeeded();await page.screenshot({path:testInfo.outputPath(`recent-${width}.png`)});
      }
    }
    await page.setViewportSize({width:1440,height:900});
    for(const wanted of [679,680]) {
      const actual=await page.locator('.page-body').evaluate(node=>node.getBoundingClientRect().width);
      const previous=page.viewportSize();await page.setViewportSize({width:previous.width+wanted-actual,height:900});
      const widths=await page.locator('.overview-period-cards').evaluate(node=>({page:document.querySelector('.page-body').getBoundingClientRect().width,columns:getComputedStyle(node).gridTemplateColumns.split(' ').length}));
      expect(widths.page).toBe(wanted);expect(widths.columns).toBe(wanted===679?1:2);
    }
    await page.getByRole('button',{name:'刷新期间指标'}).focus();await expect(page.getByRole('button',{name:'刷新期间指标'})).toBeFocused();
  } finally {await server.close();}
});


test('A real recorded read merges into the held current slot without duplicate or old pending downgrade', async ({page}) => {
  const server=await fixture();
  let recentReads=0;page.on('request',request=>{if(request.url().includes('/api/overview/recent-runs?'))recentReads++;});
  try {
    await observeStream(page);await server.send('runs 5');
    await page.route('**/api/overview/recent-runs?**',route=>route.abort());
    await page.goto(server.origin+'/overview');
    await expect(page.getByLabel('全历史最近运行')).toContainText('读取失败');
    await expect(page.locator('.overview-run')).toHaveCount(0);
    const entry=await (await page.request.get(server.origin+'/api/entry')).json();const headers={'x-agent-alfred-csrf':entry.csrf_token};
    const session=await (await page.request.post(server.origin+'/api/sessions',{headers,data:{}})).json();
    await page.evaluate(()=>window.holdIdle=true);await server.send('hold-recording');
    const accepted=await (await page.request.post(server.origin+'/api/runs',{headers,data:{session_id:session.session_id,message:'held durable overlap'}})).json();
    await server.send('wait-recording');
    const current=page.getByLabel('当前运行槽');await expect(current).toContainText('正在保存');
    const currentHref='/runs/'+encodeURIComponent(accepted.run_id);
    await expect(current.getByRole('link')).toHaveAttribute('href',currentHref);
    await server.send('release-recording');
    await page.unroute('**/api/overview/recent-runs?**');
    await page.getByRole('button',{name:'刷新最近运行'}).click();await expect(current).toContainText('已保存');
    await expect(current.getByRole('link')).toHaveText('Run '+accepted.run_id.slice(0,12)+'…');
    // New peers must refresh the label even when current identity and recorded state stay unchanged.
    await server.send('collide-run '+accepted.run_id);
    const legacyId=accepted.run_id.slice(0,12)+'-legacy-history';
    await page.getByRole('button',{name:'刷新最近运行'}).click();
    await expect(page.locator('.overview-history .overview-run')).toHaveCount(5);
    const ids=await page.locator('.overview-run').evaluateAll(rows=>rows.map(row=>row.dataset.runId));
    expect(new Set(ids).size).toBe(6);expect(ids.filter(id=>id===accepted.run_id)).toHaveLength(1);
    const legacy=page.locator('.overview-history .overview-run').filter({has:page.getByRole('link',{name:'Run '+legacyId,exact:true})});
    await expect(current.getByRole('link')).toHaveText('Run '+accepted.run_id);
    await expect(current.getByRole('link')).toHaveAttribute('href',currentHref);
    await expect(legacy.getByRole('link')).toHaveAttribute('href','/runs/'+encodeURIComponent(legacyId));
    expect(new Set(await page.locator('.overview-run>a').allTextContents()).size).toBe(6);
    const historicalIds=await page.locator('.overview-history .overview-run').evaluateAll(rows=>rows.map(row=>row.dataset.runId));
    const readsBeforeRelease=recentReads;
    const old=await page.evaluate(()=>window.lastPending);expect(old).toBeTruthy();await emit(page,'state_patch',old);
    await expect(current).toContainText('已保存');await expect(current).not.toContainText('正在保存');
    await expect.poll(()=>page.evaluate(()=>window.heldIdle?.length||0)).toBeGreaterThan(0);
    await page.evaluate(()=>{window.holdIdle=false;for(const deliver of window.heldIdle.splice(0))deliver();});
    await expect(current.locator('.overview-run')).toHaveCount(0);
    await expect(page.locator('.overview-history .overview-run')).toHaveCount(5);
    expect(await page.locator('.overview-history .overview-run').evaluateAll(rows=>rows.map(row=>row.dataset.runId))).toEqual(historicalIds);
    expect(recentReads).toBe(readsBeforeRelease);
    expect(historicalIds).not.toContain(accepted.run_id);
    await page.getByRole('button',{name:'刷新最近运行'}).click();
    await expect(page.locator('.overview-history .overview-run').first()).toHaveAttribute('data-run-id',accepted.run_id);
    expect(recentReads).toBe(readsBeforeRelease+1);
    const links=page.locator('.overview-history .overview-run>a');
    expect(new Set(await links.allTextContents()).size).toBe(5);
    await expect(links.first()).toHaveAttribute('href',currentHref);
  } finally {await server.send('release-recording');await server.close();}
});
