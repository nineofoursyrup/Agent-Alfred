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
    const page=await context.newPage();await open(page,s.origin);let count=0;
    await page.route('**/api/runs',async route=>{if(route.request().method()==='POST'){count++;await route.fetch();await route.abort();}else await route.continue();});
    await selected(page).getByRole('button',{name:'测试真实调用（可能计费）',exact:true}).click();
    await expect(selected(page).getByText(/探针受理结果未确认/).first()).toBeVisible();
    await expect(selected(page).getByRole('link',{name:/查看探针 Run/})).toHaveCount(0);
    await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
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
