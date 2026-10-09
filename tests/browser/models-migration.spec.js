import {test,expect} from '@playwright/test';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {once} from 'node:events';

async function server() {
  const child=spawn('.venv/bin/python',['tests/browser/settings_server.py']);
  const lines=createInterface({input:child.stdout});let errors='';child.stderr.on('data',d=>errors+=d);
  const value=await Promise.race([once(lines,'line').then(([line])=>JSON.parse(line)),once(child,'exit').then(()=>{throw new Error(errors);})]);
  return {...value,async close(){const done=once(child,'exit');child.kill('SIGTERM');await done;lines.close();if(child.exitCode!==0)throw new Error(errors);}};
}
const selected=page=>page.locator("[data-model='opencode-go:deepseek-v4-flash']");
async function open(page, origin) {
  await page.goto(origin+'/models');
  await selected(page).getByText('模型详情',{exact:true}).click();
}
async function saved(page) {
  const data=await (await page.request.get('/api/models')).json();
  return data.endpoints.flatMap(group=>group.models).find(model=>model.endpoint_id==='opencode-go' && model.model_id==='deepseek-v4-flash');
}

for (const lost of [false,true])
test(`S11 model ${lost?'unknown':'pending'} save protects a successor equal to its old saved value`,async({browser})=>{
  const s=await server(),context=await browser.newContext({baseURL:s.origin});let release=()=>{};
  try{
    const page=await context.newPage();await open(page,s.origin);const row=selected(page);
    const input=row.getByRole('textbox',{name:'显示名',exact:true}),save=row.getByRole('button',{name:'保存显示名',exact:true});
    await input.fill('A');await save.click();await expect(row.getByText('已确认保存',{exact:true})).toBeVisible();
    let received;const arrived=new Promise(resolve=>received=resolve),gate=new Promise(resolve=>release=resolve);const posts=[];
    await page.route('**/api/settings',async route=>{posts.push(route.request().postDataJSON());const response=await route.fetch();received();await gate;if(lost)await route.abort();else await route.fulfill({response});});
    await input.fill('B');await save.click();await arrived;
    if(lost){release();await expect(row.getByText('保存结果未确认。先核对当前值；不会自动重送。',{exact:true})).toBeVisible();}
    await input.fill('A');
    await page.locator('nav a[href="/overview"]').click();
    await expect(page.getByRole('button',{name:'留在此页',exact:true})).toBeFocused();
    await page.keyboard.press('Escape');await expect(input).toHaveValue('A');
    if(lost){await page.getByRole('button',{name:'核对当前模型设置',exact:true}).click();await expect(row.getByText('当前保存值：B',{exact:true})).toBeVisible();await expect(input).toHaveValue('A');}
    await input.fill('B'); // The submitted value itself is no unsaved successor.
    await page.locator('nav a[href="/overview"]').click();
    await expect(page).toHaveURL(/\/overview$/);await expect(page.getByRole('dialog')).toHaveCount(0);
    release();expect(posts.map(p=>[p.display_name,p.expected_revision])).toEqual([['B',1]]);
    expect((await saved(page)).display_name_override).toBe('B');
    expect((await (await fetch(s.control)).json()).model_calls).toHaveLength(0);
  }finally{release();await context.close();await s.close();}
});

test('S08 real catalog label and explicit clearing survive HTTP read and page reentry',async({browser})=>{
  const s=await server(), context=await browser.newContext({baseURL:s.origin});
  try {
    const page=await context.newPage();await open(page,s.origin);const row=selected(page);
    await expect(row.getByRole('heading',{name:'目录模型名称',exact:true})).toBeVisible();
    await expect(row.getByRole('textbox',{name:'显示名',exact:true})).toHaveValue('');
    const input=row.getByRole('textbox',{name:'显示名',exact:true});
    await input.fill('真实用户显示名');await row.getByRole('button',{name:'保存显示名',exact:true}).click();
    await expect.poll(async()=> (await saved(page)).display_name_override).toBe('真实用户显示名');
    await input.fill('');await row.getByRole('button',{name:'保存显示名',exact:true}).click();
    await expect.poll(async()=> (await saved(page)).display_name_override).toBeNull();
    await expect(row.getByRole('heading',{name:'目录模型名称',exact:true})).toBeVisible();
    const price=row.getByRole('textbox',{name:'output',exact:true});
    await price.fill('0');await row.getByRole('button',{name:'保存 output',exact:true}).click();
    await expect.poll(async()=> (await saved(page)).price_override?.output).toBe('0');
    await price.fill('');await row.getByRole('button',{name:'保存 output',exact:true}).click();
    await expect.poll(async()=> (await saved(page)).price_override).toBeNull();
    await page.locator('nav a[href="/connections"]').click();
    await page.locator('nav a[href="/models"]').click();
    await row.getByText('模型详情',{exact:true}).click();
    await expect(input).toHaveValue('');await expect(price).toHaveValue('');
    expect((await saved(page)).display_name).toBe('目录模型名称');
    expect((await (await fetch(s.control)).json()).model_calls).toHaveLength(0);
  }finally{await context.close();await s.close();}
});

test('S08 real two-tab conflict retains baseline and requires explicit adoption',async({browser})=>{
  const s=await server(), context=await browser.newContext({baseURL:s.origin});
  try {
    const a=await context.newPage(),b=await context.newPage();await open(a,s.origin);await open(b,s.origin);
    const first=selected(a),second=selected(b),draft=first.getByRole('textbox',{name:'显示名',exact:true});
    await draft.fill('原标签草稿');await second.getByRole('textbox',{name:'显示名',exact:true}).fill('另一标签已保存');
    await second.getByRole('button',{name:'保存显示名',exact:true}).click();
    await expect.poll(async()=> (await saved(b)).display_name_override).toBe('另一标签已保存');
    await first.getByRole('button',{name:'保存显示名',exact:true}).click();
    await expect(first.getByText(/stale_revision/)).toBeVisible();
    await expect(draft).toHaveValue('原标签草稿');
    await expect(first.getByText('当前保存值：另一标签已保存',{exact:true})).toBeVisible();
    await expect(first.getByRole('button',{name:'保存显示名',exact:true})).toBeDisabled();
    await first.getByRole('button',{name:'基于当前版本继续编辑',exact:true}).click();
    await first.getByRole('button',{name:'保存显示名',exact:true}).click();
    await expect.poll(async()=> (await saved(a)).display_name_override).toBe('原标签草稿');
  }finally{await context.close();await s.close();}
});

test('S08 SPEC F1 lost saved receipt requires a post-failure read before explicit adoption',async({browser})=>{
  const s=await server(),context=await browser.newContext({baseURL:s.origin});let release=()=>{};
  try {
    const page=await context.newPage();await open(page,s.origin);const row=selected(page);
    const input=row.getByRole('textbox',{name:'显示名',exact:true}),save=row.getByRole('button',{name:'保存显示名',exact:true});
    const adopt=row.getByRole('button',{name:'基于当前版本继续编辑',exact:true});
    const refresh=page.getByRole('button',{name:'核对当前模型设置',exact:true});
    await input.fill('A');await save.click();await expect(row.getByText('已确认保存',{exact:true})).toBeVisible();
    let received,held,got,mode='hold';const posts=[],reads=[];
    const barrier=()=>{received=new Promise(resolve=>got=resolve);held=new Promise(resolve=>release=resolve);};barrier();
    await page.route('**/api/models',async route=>{
      reads.push(mode);
      if(mode==='fail'){await route.abort();return;}
      const response=await route.fetch();got();await held;await route.fulfill({response}).catch(()=>{});
    });
    await page.route('**/api/settings',async route=>{
      const request=route.request().postDataJSON();const response=await route.fetch();posts.push({request,status:response.status()});
      if(request.display_name==='B')await route.abort();else await route.fulfill({response});
    });
    await refresh.click();await received; // This genuine revision 1 read predates the failure.
    await input.fill('B');await save.click();
    await expect(row.getByText('保存结果未确认。先核对当前值；不会自动重送。',{exact:true})).toBeVisible();
    expect((await saved(page)).display_name_override).toBe('B');
    expect(reads).toEqual(['hold']);await expect(adopt).toBeDisabled();
    const oldRead=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/models');release();await oldRead;
    await expect(adopt).toBeDisabled();await expect(row.getByText('当前保存值：A',{exact:true})).toBeVisible();
    mode='fail';await refresh.click();await expect(page.getByText(/模型设置读取失败/)).toBeVisible();
    await expect(adopt).toBeDisabled();await expect(input).toHaveValue('B');
    mode='hold';barrier();await refresh.click();await received;
    await input.fill('C');await input.evaluate(el=>el.setSelectionRange(0,1));release();
    await expect(row.getByText('当前保存值：B',{exact:true})).toBeVisible();
    await expect(input).toHaveValue('C');await expect(input).toBeFocused();
    expect(await input.evaluate(el=>[el.selectionStart,el.selectionEnd])).toEqual([0,1]);
    await expect(row.locator('.model-row-state')).toContainText('保存结果未确认');await expect(save).toBeDisabled();
    await expect(adopt).toBeEnabled();expect(posts).toHaveLength(1);
    await adopt.click();await expect(input).toHaveValue('C');expect(posts).toHaveLength(1);
    await save.click();await expect(row.getByText('已确认保存',{exact:true})).toBeVisible();
    expect(posts.map(p=>[p.request.display_name,p.request.expected_revision,p.status])).toEqual([['B',1,200],['C',2,200]]);
    expect((await saved(page)).display_name_override).toBe('C');
    expect((await (await fetch(s.control)).json()).model_calls).toHaveLength(0);
  }finally{release();await context.close();await s.close();}
});

test('S08 SPEC F1 real conflict with a failed comparison keeps its baseline even when values match',async({browser})=>{
  const s=await server(),context=await browser.newContext({baseURL:s.origin});
  try {
    const a=await context.newPage(),b=await context.newPage();await open(a,s.origin);await open(b,s.origin);
    const row=selected(a),input=row.getByRole('textbox',{name:'显示名',exact:true});
    const save=row.getByRole('button',{name:'保存显示名',exact:true}),adopt=row.getByRole('button',{name:'基于当前版本继续编辑',exact:true});
    await input.fill('相同值');await selected(b).getByRole('textbox',{name:'显示名',exact:true}).fill('相同值');
    await selected(b).getByRole('button',{name:'保存显示名',exact:true}).click();
    await expect(selected(b).getByText('已确认保存',{exact:true})).toBeVisible();
    let fail=true;const posts=[];
    await a.route('**/api/models',async route=>{if(fail)await route.abort();else await route.continue();});
    await a.route('**/api/settings',async route=>{const request=route.request().postDataJSON();const response=await route.fetch();posts.push({request,status:response.status()});await route.fulfill({response});});
    await save.click();await expect(a.getByText(/模型设置读取失败/)).toBeVisible();
    await expect(row.getByText(/编辑基线：空（无覆盖） · revision 0/)).toBeVisible();
    await expect(input).toHaveValue('相同值');await expect(adopt).toBeDisabled();await expect(save).toBeDisabled();
    expect(posts.map(p=>[p.request.expected_revision,p.status])).toEqual([[0,409]]);
    fail=false;await a.getByRole('button',{name:'核对当前模型设置',exact:true}).click();
    await expect(row.getByText('当前保存值：相同值',{exact:true})).toBeVisible();
    await expect(row.getByText(/编辑基线：空（无覆盖） · revision 0；当前 revision 1/)).toBeVisible();
    await expect(row.getByText(/stale_revision/)).toBeVisible();await expect(save).toBeDisabled();
    await expect(adopt).toBeEnabled();expect(posts).toHaveLength(1);
    await adopt.click();await input.fill('明确重提');await save.click();
    await expect(row.getByText('已确认保存',{exact:true})).toBeVisible();
    expect(posts.map(p=>[p.request.expected_revision,p.status])).toEqual([[0,409],[1,200]]);
    expect((await saved(a)).display_name_override).toBe('明确重提');
  }finally{await context.close();await s.close();}
});

test('S08 SPEC F1 real instance change retires the old comparison read and qualification',async({browser})=>{
  let s=await server();const context=await browser.newContext({baseURL:s.origin});let release=()=>{};
  try {
    const page=await context.newPage();await open(page,s.origin);const row=selected(page);
    const input=row.getByRole('textbox',{name:'显示名',exact:true}),save=row.getByRole('button',{name:'保存显示名',exact:true});
    const adopt=row.locator('.setting-field').first().getByRole('button',{name:'基于当前版本继续编辑',exact:true});
    const refresh=page.getByRole('button',{name:'核对当前模型设置',exact:true});
    const before=await (await page.request.get('/api/entry')).json();let posts=0;
    await page.route('**/api/settings',async route=>{posts++;await route.fetch();await route.abort();});
    await input.fill('保留跨实例草稿');await save.click();await expect(adopt).toBeDisabled();
    await refresh.click();await expect(adopt).toBeEnabled();
    let received,reads=0,mode='hold';const captured=new Promise(resolve=>received=resolve),held=new Promise(resolve=>release=resolve);
    await page.route('**/api/models',async route=>{
      reads++;if(mode==='fail'){await route.abort();return;}
      const response=await route.fetch();received();await held;await route.fulfill({response}).catch(()=>{});
    });
    await refresh.click();await captured;mode='fail';await s.close();s=await server();
    expect(before.instance_id).toBeTruthy();
    expect((await (await page.request.get('/api/entry')).json()).instance_id).not.toBe(before.instance_id);
    await expect.poll(()=>reads).toBeGreaterThan(1);
    await expect(page.getByText(/模型设置读取失败/)).toBeVisible();await expect(adopt).toBeDisabled();
    const late=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/models');release();await late;
    await expect(adopt).toBeDisabled();await expect(input).toHaveValue('保留跨实例草稿');expect(posts).toBe(1);
    await page.unroute('**/api/models');await refresh.click();await expect(adopt).toBeEnabled();
    await expect(row.getByText('当前保存值：空（无覆盖）',{exact:true}).first()).toBeVisible();
    await expect(input).toHaveValue('保留跨实例草稿');await expect(save).toBeDisabled();expect(posts).toBe(1);
  }finally{release();await context.close();await s.close();}
});

test('S08 saved model probe preserves drafts and links its real Attempt and cost',async({browser})=>{
  const s=await server(),context=await browser.newContext({baseURL:s.origin});
  try {
    const page=await context.newPage();await open(page,s.origin);const row=selected(page);
    const input=row.getByRole('textbox',{name:'显示名',exact:true});await input.fill('未保存探针草稿');
    await row.getByRole('combobox',{name:'线路形状',exact:true}).selectOption('anthropic');
    const before=await saved(page);let payload;
    page.on('request',request=>{if(new URL(request.url()).pathname==='/api/runs' && request.method()==='POST')payload=request.postDataJSON();});
    await row.getByRole('button',{name:'测试真实调用（可能计费）',exact:true}).click();
    const link=row.getByRole('link',{name:/查看探针 Run/});await expect(link).toBeVisible();
    const id=decodeURIComponent(new URL(await link.getAttribute('href'),s.origin).pathname.split('/').pop());
    await expect(row.getByText(/探针已受理；不代表完成/).first()).toBeVisible();
    await expect(input).toHaveValue('未保存探针草稿');
    expect(payload).toEqual({purpose:'inference_probe',message:'inference probe',endpoint_id:'opencode-go',model_id:'deepseek-v4-flash'});
    expect((await saved(page)).wire_style).toBe(before.wire_style);
    await expect.poll(async()=> (await (await fetch(s.control)).json()).model_calls.length).toBe(1);
    const evidence=await (await page.request.get('/api/run-evidence?run_id='+encodeURIComponent(id))).json();
    expect(evidence.attempts[0].attempt_id).toBe('settings-probe');
    expect(evidence.attempts[0].cost.state).toBe('estimated');
    await link.click();await page.getByRole('button',{name:'放弃并离开',exact:true}).click();
    await expect(page).toHaveURL(new RegExp('/runs/'+id));
    await expect(page.locator('#page')).toContainText('Attempt');
    await expect(page.locator('#page')).toContainText('USD 0.00088');
    expect((await (await fetch(s.control)).json()).model_calls).toHaveLength(1);
  }finally{await context.close();await s.close();}
});

test('S08 a lost real accepted probe response never replays on focus or reentry',async({browser})=>{
  const s=await server(),context=await browser.newContext({baseURL:s.origin});
  try {
    const page=await context.newPage();await open(page,s.origin);const row=selected(page);let count=0;
    await page.route('**/api/runs',async route=>{if(route.request().method()==='POST'){count++;await route.fetch();await route.abort();}else await route.continue();});
    await selected(page).getByRole('button',{name:'测试真实调用（可能计费）',exact:true}).click();
    await expect(selected(page).getByText(/探针受理结果未确认/).first()).toBeVisible();
    await expect(selected(page).getByRole('link',{name:/查看探针 Run/})).toHaveCount(0);
    await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
    await expect(row.getByRole('link',{name:/查看探针 Run/})).toHaveCount(0);
    await row.getByRole('link',{name:'核对运行记录',exact:true}).click();
    await expect(page).toHaveURL(/\/runs\?filter=system$/);
    await expect(page.getByRole('region',{name:'运行列表'}).getByRole('link',{name:'查看运行',exact:true})).toHaveCount(1);
    await page.locator('nav a[href="/ops"]').click();
    await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(1);
    await page.locator('nav a[href="/connections"]').click();await page.locator('nav a[href="/models"]').click();
    expect(count).toBe(1);expect((await (await fetch(s.control)).json()).model_calls).toHaveLength(1);
  }finally{await context.close();await s.close();}
});

test('S08 catalog refresh stays one request and settings remain usable in four viewports',async({browser},testInfo)=>{
  const s=await server(),context=await browser.newContext({baseURL:s.origin,hasTouch:true});
  try {
    const page=await context.newPage();await open(page,s.origin);
    let release,captured,count=0;const held=new Promise(r=>release=r),received=new Promise(r=>captured=r);
    await page.route('**/api/models?*',async route=>{if(new URL(route.request().url()).searchParams.get('refresh')==='1'){count++;const response=await route.fetch();captured();await held;await route.fulfill({response});}else await route.continue();});
    const button=page.locator('[data-endpoint="opencode-go"]').getByRole('button',{name:'刷新目录',exact:true});
    await button.click();await received;
    await expect(page.locator('[data-endpoint="opencode-go"]').getByRole('button',{name:'目录读取中…',exact:true})).toBeDisabled();
    release();await expect(button).toBeEnabled();expect(count).toBe(1);
    const row=selected(page);await row.getByRole('textbox',{name:'显示名',exact:true}).fill('长名称 '.repeat(20));
    const dimensions=[];
    for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
      await page.setViewportSize({width,height});await expect(row.getByRole('textbox',{name:'显示名',exact:true})).toHaveValue('长名称 '.repeat(20));
      await row.getByRole('button',{name:'保存显示名',exact:true}).scrollIntoViewIfNeeded();
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
      dimensions.push({viewport:[width,height],central:await page.locator('#page').boundingBox()});
      await page.screenshot({path:testInfo.outputPath(`models-${width}.png`)});
    }
    console.log('S08 Models central widths',JSON.stringify(dimensions));
  }finally{await context.close();await s.close();}
});

test('S08 rejected unpin keeps the original dirty draft',async({browser})=>{
  const s=await server(),context=await browser.newContext({baseURL:s.origin});
  try {
    const page=await context.newPage();await page.goto(s.origin+'/models');
    const row=page.locator("[data-model='opencode-go:qwen3.7-max']");
    await row.getByText('模型详情',{exact:true}).click();
    await row.getByRole('button',{name:'钉选',exact:true}).click();
    await expect(row.getByRole('textbox',{name:'显示名',exact:true})).toBeVisible();
    await row.getByRole('textbox',{name:'显示名',exact:true}).fill('拒绝后必须保留');
    await page.route('**/api/settings',async route=>{if(route.request().postDataJSON().op==='unpin')await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({code:'mutation_in_flight'})});else await route.continue();});
    await row.getByRole('button',{name:'取消钉选',exact:true}).click();
    await page.getByRole('button',{name:'放弃并离开',exact:true}).click();
    await expect(row.getByRole('textbox',{name:'显示名',exact:true})).toHaveValue('拒绝后必须保留');
  }finally{await context.close();await s.close();}
});

test('S08 each saved field is independent and uses the public HTTP value',async({browser})=>{
  const s=await server(),context=await browser.newContext({baseURL:s.origin});
  try {
    const page=await context.newPage();await open(page,s.origin);const row=selected(page);const posts=[];
    page.on('request',r=>{if(new URL(r.url()).pathname==='/api/settings' && r.method()==='POST')posts.push(r.postDataJSON());});
    for(const dimension of ['uncached_input','cache_read','cache_write','output'])await row.getByRole('textbox',{name:dimension,exact:true}).fill('0');
    await row.getByRole('combobox',{name:'线路形状',exact:true}).selectOption('anthropic');
    expect(posts).toHaveLength(0);
    await row.getByRole('button',{name:'保存线路',exact:true}).click();
    await expect.poll(async()=>(await saved(page)).wire_style).toBe('anthropic');
    expect((await saved(page)).price_override).toBeNull();
    for(const [index,dimension] of ['uncached_input','cache_read','cache_write','output'].entries()) {
      await row.getByRole('button',{name:'保存 '+dimension,exact:true}).click();
      await expect.poll(async()=>(await saved(page)).price_override?.[dimension]).toBe('0');
      expect(posts).toHaveLength(index+2);
    }
    expect(posts.map(p=>p.op)).toEqual(['style','price','price','price','price']);
    expect(posts.slice(1).map(p=>p.dimension)).toEqual(['uncached_input','cache_read','cache_write','output']);
    await expect(row.getByText(/未验证声明/)).toBeVisible();
    expect((await (await fetch(s.control)).json()).model_calls).toHaveLength(0);
  }finally{await context.close();await s.close();}
});

test('S08 STD01 retired save cannot clear a successor request after real Stream reconnect',async({browser})=>{
  const s=await server(),context=await browser.newContext({baseURL:s.origin});
  let releaseA=()=>{},releaseB=()=>{};
  try {
    const page=await context.newPage();await open(page,s.origin);const row=selected(page);
    const input=row.getByRole('textbox',{name:'显示名',exact:true});
    const save=row.getByRole('button',{name:'保存显示名',exact:true});
    const posts=[];let gotA,gotB;
    const recvA=new Promise(resolve=>gotA=resolve),recvB=new Promise(resolve=>gotB=resolve);
    const heldA=new Promise(resolve=>releaseA=resolve),heldB=new Promise(resolve=>releaseB=resolve);
    await page.route('**/api/settings',async route=>{
      const request=route.request().postDataJSON();posts.push(request);
      const response=await route.fetch();expect(response.status()).toBe(200);
      if(request.display_name==='A'){gotA();await heldA;}
      if(request.display_name==='B'){gotB();await heldB;}
      await route.fulfill({response}).catch(()=>{});
    });
    await input.fill('A');await save.click();await recvA;
    await context.setOffline(true);
    await expect(row.getByText(/连接中断，保存结果未确认/)).toBeVisible();
    await context.setOffline(false);
    await expect(row.getByText('当前保存值：A',{exact:true})).toBeVisible();
    await row.getByRole('button',{name:'基于当前版本继续编辑',exact:true}).click();
    await input.fill('B');await save.click();await recvB;await expect(save).toBeDisabled();
    const arrived=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/settings' && r.request().postDataJSON().display_name==='A');
    releaseA();await arrived;
    await input.fill('C');
    await expect(save).toBeDisabled();
    await expect(row.locator('.model-row-state')).toContainText('提交中');
    expect(posts.map(p=>[p.display_name,p.expected_revision])).toEqual([['A',0],['B',1]]);
    releaseB();
    await expect(row.getByText('已确认保存；还有新编辑',{exact:true})).toBeVisible();
    await expect(input).toHaveValue('C');await expect(save).toBeEnabled();
    expect((await saved(page)).display_name_override).toBe('B');
    expect(posts).toHaveLength(2);
    expect((await (await fetch(s.control)).json()).model_calls).toHaveLength(0);
  }finally{releaseA();releaseB();await context.setOffline(false);await context.close();await s.close();}
});

test('S08 STD02 model action focus survives refresh and completion without stealing a new intent',async({browser})=>{
  const s=await server(),context=await browser.newContext({baseURL:s.origin});let release=()=>{};
  try {
    const page=await context.newPage();await open(page,s.origin);const row=selected(page);
    const input=row.getByRole('textbox',{name:'显示名',exact:true});
    const save=row.getByRole('button',{name:'保存显示名',exact:true});
    const other=page.locator("[data-model='opencode-go:qwen3.7-max']");
    await other.getByText('模型详情',{exact:true}).click();await other.getByRole('button',{name:'钉选',exact:true}).click();
    await expect(other.getByRole('textbox',{name:'显示名',exact:true})).toBeVisible();
    for(const button of [save,other.getByRole('button',{name:'保存显示名',exact:true}),row.getByRole('button',{name:'保存 output',exact:true}),row.getByRole('button',{name:'指派为主模型',exact:true}),row.getByRole('button',{name:'测试真实调用（可能计费）',exact:true}),page.locator('[data-endpoint="opencode-go"]').getByRole('button',{name:'刷新目录',exact:true})]) {
      await button.focus();
      const reread=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/models');
      await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await reread;
      await expect(button).toBeFocused();
    }
    let entered,posts=0;let received=new Promise(resolve=>entered=resolve);let held=new Promise(resolve=>release=resolve);
    await page.route('**/api/settings',async route=>{posts++;const response=await route.fetch();entered();await held;await route.fulfill({response}).catch(()=>{});});
    await input.fill('focused action');await save.click();await received;await expect(save).toBeDisabled();
    release();await expect(row.getByText('已确认保存',{exact:true})).toBeVisible();await expect(save).toBeFocused();
    received=new Promise(resolve=>entered=resolve);held=new Promise(resolve=>release=resolve);
    await input.fill('MainBar owns focus');await save.click();await received;
    const chat=page.getByRole('button',{name:'新建会话',exact:true});await chat.focus();
    release();await expect(row.getByText('已确认保存',{exact:true})).toBeVisible();await expect(chat).toBeFocused();
    received=new Promise(resolve=>entered=resolve);held=new Promise(resolve=>release=resolve);
    await input.fill('page has retired');await save.click();await received;
    await input.fill('new unsaved input before retirement');
    await page.locator('nav a[href="/connections"]').click();await page.getByRole('button',{name:'放弃并离开',exact:true}).click();
    const reread=page.getByRole('button',{name:'重新读取 .env',exact:true});await reread.focus();
    const finished=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/settings');release();await finished;
    await expect(reread).toBeFocused();expect(posts).toBe(3);
    expect((await (await fetch(s.control)).json()).model_calls).toHaveLength(0);
  }finally{release();await context.close();await s.close();}
});


test('S11 clear final price changes the next probe and Ops while the old snapshot stays fixed',async({browser})=>{
  const s=await server(),context=await browser.newContext({baseURL:s.origin});
  try{
    const page=await context.newPage();await open(page,s.origin);const row=selected(page);
    const price=row.getByRole('textbox',{name:'output',exact:true}),save=row.getByRole('button',{name:'保存 output',exact:true});
    await price.fill('0');await save.click();await expect.poll(async()=>(await saved(page)).price_override?.output).toBe('0');
    async function probe(){
      const accepted=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/runs'&&r.request().method()==='POST');
      await row.getByRole('button',{name:'测试真实调用（可能计费）',exact:true}).click();
      const response=await accepted;expect(response.status()).toBe(202);const {run_id}=await response.json();
      await expect.poll(async()=>(await(await page.request.get('/api/runs?filter=all')).json()).runs.some(run=>run.run_id===run_id)).toBe(true);
      return run_id;
    }
    const first=await probe();
    const {csrf_token}=await(await page.request.get('/api/entry')).json();
    const snapshot=await(await page.request.post('/api/ops/snapshots',{headers:{'x-agent-alfred-csrf':csrf_token},data:{range:'all',timezone:'UTC'}})).json();
    const detailPath='/api/ops/detail?'+new URLSearchParams({snapshot_id:snapshot.snapshot_id,run_id:first});
    const frozen=await(await page.request.get(detailPath)).json();
    expect(frozen.run.attempts[0].cost.state).toBe('estimated');
    // Fixture emits 1,000 uncached + 1,000 output tokens: .22/million input, zero output override.
    expect(frozen.run.attempts[0].cost.amount).toBe('0.00022');
    expect(frozen.run.attempts[0].cost.price_components.find(part=>part.dimension==='output').unit_price).toBe('0');
    await price.fill('');await save.click();await expect.poll(async()=>(await saved(page)).price_override).toBeNull();
    const second=await probe();expect(second).not.toBe(first);
    expect(await(await page.request.get(detailPath)).json()).toEqual(frozen);
    await row.getByRole('link',{name:'查看探针 Run '+second,exact:true}).click();
    await expect(page.getByRole('region',{name:'运行摘要',exact:true})).toContainText(second);
    await expect(page.locator('#page')).toContainText('USD 0.00088');
    await page.locator('nav a[href="/ops"]').click();
    await page.getByRole('button',{name:'查看账目 '+second,exact:true}).click();
    await expect(page.getByRole('table',{name:'模型 Attempt'})).toContainText('settings-probe');
    await expect(page.getByRole('table',{name:'模型 Attempt'})).toContainText('估算费用 USD 0.00088');
    const before=await page.getByRole('region',{name:'当前账目快照'}).textContent();
    await page.getByRole('link',{name:'进入运行过程（保留账目快照）',exact:true}).click();
    await page.getByRole('link',{name:'返回来源',exact:true}).click();
    await expect(page.getByRole('region',{name:'当前账目快照'})).toHaveText(before);
    await expect(page.getByRole('link',{name:'进入运行过程（保留账目快照）',exact:true})).toBeFocused();
    expect((await(await fetch(s.control)).json()).model_calls).toHaveLength(2);
    expect(await(await page.request.get(detailPath)).json()).toEqual(frozen);
  }finally{await context.close();await s.close();}
});
