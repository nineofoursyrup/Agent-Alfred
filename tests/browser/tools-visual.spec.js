import {test,expect} from '@playwright/test';
import {memoryServer} from './memory-server.js';
import {writeFile} from 'node:fs/promises';

test('S07 four viewports, shell and measured central boundaries retain full text and keyboard controls',async({browser},info)=>{
  test.setTimeout(45000);
  const server=await memoryServer({script:'tests/browser/tools_server.py'}),observations=[];
  try {
    for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
      const context=await browser.newContext({viewport:{width,height},hasTouch:width<1100,deviceScaleFactor:1,reducedMotion:'no-preference'});
      try {
        const page=await context.newPage(),failed=[];
        page.on('requestfailed',request=>failed.push(request.url()));
        await page.goto(server.origin+'/tools');
        await expect(page.getByText('当前目录已核对；连接观测、保存与生效分别显示。')).toBeVisible();
        await page.screenshot({path:info.outputPath(`tools-normal-${width}.png`)});
        const integration=page.getByRole('region',{name:'集成 · tavily',exact:true});
        await expect(integration).toContainText('web_search');
        const row=integration.locator('article');
        const toggle=row.getByRole('button',{name:'展开详情',exact:true});
        await toggle.focus();await page.keyboard.press('Enter');
        await row.getByRole('combobox').selectOption('allowed');
        await row.getByRole('button',{name:'收起详情',exact:true}).click();
        await expect(row.getByLabel('工具状态摘要')).toContainText('未保存草稿：allowed');
        await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
        await page.locator('#mainbar-title').focus();await page.keyboard.press('Escape');
        await expect(row.getByLabel('工具状态摘要')).toContainText('未保存草稿：allowed');
        const long=page.locator('article').filter({has:page.getByRole('heading',{name:'read_persona_looks_builtin',exact:true})});
        await expect(page.getByRole('region',{name:'内置工具',exact:true}).getByRole('heading',{name:'read_persona_looks_builtin',exact:true})).toHaveCount(0);
        await long.getByRole('button',{name:'展开详情',exact:true}).click();
        await expect(long.locator('.tool-description')).toContainText('完整描述结束标识。');
        await expect(long.locator('.tool-detail .tool-identity')).toContainText('同名前缀'.repeat(40));
        await long.getByRole('button',{name:'收起详情',exact:true}).focus();
        await page.screenshot({path:info.outputPath(`tools-long-draft-${width}.png`)});
        const metrics=await page.evaluate(()=>({
          viewport:[innerWidth,innerHeight],touch:navigator.maxTouchPoints,bodyOverflow:document.documentElement.scrollWidth-innerWidth,
          central:document.querySelector('.page-body').getBoundingClientRect().width,
          focusOutline:getComputedStyle(document.activeElement).outlineWidth,
          fonts:Object.fromEntries([['name','.tool-row h3'],['identity','.tool-identity'],['body','.tool-description'],['button','.tool-row button']].map(([role,selector])=>[role,getComputedStyle(document.querySelector(selector)).fontSize])),
          color:getComputedStyle(document.querySelector('.tools-page')).color,background:getComputedStyle(document.body).backgroundColor,
          transitions:[...document.querySelectorAll('.tools-page *')].flatMap(n=>[null,'::before','::after'].map(pseudo=>getComputedStyle(n,pseudo))).filter(style=>style.transitionDuration!=='0s').map(style=>({duration:style.transitionDuration,property:style.transitionProperty})),
        }));
        expect(metrics.bodyOverflow).toBeLessThanOrEqual(0);
        expect(metrics.fonts).toEqual({name:'12.5px',identity:'11px',body:'13px',button:'11.5px'});
        for(const transition of metrics.transitions){expect(transition.property).toBe('opacity');expect(Number.parseFloat(transition.duration)).toBeLessThanOrEqual(.18);}
        expect(metrics.focusOutline).toBe('3px');expect(failed).toEqual([]);
        observations.push(metrics);
      }finally{await context.close();}
    }
    const context=await browser.newContext({viewport:{width:1440,height:900}});
    try {
      const page=await context.newPage();await page.goto(server.origin+'/tools');
      await expect(page.getByRole('region',{name:'内置工具',exact:true})).toBeVisible();
      for(const width of [1099,1100]) {
        await page.setViewportSize({width,height:900});
        await expect.poll(()=>page.locator('.site-header').evaluate(n=>n.getBoundingClientRect().width)).toBe(width===1100?232:0);
        const geometry=await page.evaluate(()=>({viewport:innerWidth,nav:document.querySelector('.site-header').getBoundingClientRect().width,mainbar:document.querySelector('#mainbar').getBoundingClientRect().width,central:document.querySelector('.page-body').getBoundingClientRect().width}));
        if(width===1100){expect(geometry.nav).toBe(232);expect(geometry.mainbar).toBe(320);}
        await page.screenshot({path:info.outputPath(`tools-shell-${width}.png`)});observations.push(geometry);
      }
      await page.setViewportSize({width:1440,height:900});
      for(const target of [679,680]) {
        const current=await page.locator('.page-body').boundingBox();
        await page.setViewportSize({width:page.viewportSize().width+target-current.width,height:900});
        const measured=await page.locator('.page-body').boundingBox();expect(measured.width).toBe(target);
        const columns=await page.locator('.tool-status').first().evaluate(n=>getComputedStyle(n).gridTemplateColumns.split(' ').length);
        expect(columns).toBe(target===679?1:2);
        await page.screenshot({path:info.outputPath(`tools-central-${target}.png`)});observations.push({central:measured.width,viewport:page.viewportSize(),columns});
      }
    } finally {await context.close();}
    const evidence=JSON.stringify({browser:browser.version(),observations,native_zoom:'NOT RUN — coordinator',native_ime:'NOT RUN — coordinator',mobile:'Chromium viewport/touch simulation only',bfcache:'historical BLOCKED preserved'},null,2);
    await writeFile(info.outputPath('geometry-fonts-and-scope.json'),evidence);
    await info.attach('geometry-fonts-and-scope',{body:evidence,contentType:'application/json'});
  } finally {await server.close();}
});

test('V01 public recipes keep pulse opt-in, selection visible and reduced motion still',async({page})=>{
  await page.emulateMedia({reducedMotion:'no-preference'});
  await page.goto('/tools');
  // A consumer fixture exercises public CSS without binding production state.
  await page.locator('#page').evaluate(host=>{
    const fixture=document.createElement('section');fixture.setAttribute('aria-label','公共配方检查');fixture.className='sy-panel';
    fixture.innerHTML=`<header class="sy-panel-header"><h2 class="sy-panel-eyebrow">公共配方</h2></header>
      <div class="sy-panel-body">
        <span class="sy-status-dot sy-tone-success sy-pulse" aria-hidden="true" data-dot="pulsing"></span><span>已连接（测试事实）</span>
        <span class="sy-status-dot" aria-hidden="true" data-dot="static"></span><span>未知（测试事实）</span>
        <p class="sy-tone-error">失败（测试事实）</p>
        <button class="sy-tab" aria-pressed="false">查看测试类别</button>
        <div class="sy-row" data-selected="false"><span class="sy-row-content">运行摘要（测试事实）</span><span class="sy-row-meta">回复完成 · 未保存</span></div>
      </div>`;
    host.replaceChildren(fixture);
  });
  const fixture=page.getByRole('region',{name:'公共配方检查'}),tab=fixture.getByRole('button',{name:'查看测试类别'}),row=fixture.locator('.sy-row');
  const styles=()=>fixture.evaluate(host=>{
    const dot=getComputedStyle(host.querySelector('[data-dot=pulsing]')),staticDot=getComputedStyle(host.querySelector('[data-dot=static]'));
    const selected=['.sy-tab','.sy-row'].map(selector=>{const style=getComputedStyle(host.querySelector(selector),'::before');return {opacity:style.opacity,property:style.transitionProperty,duration:style.transitionDuration};});
    return {pulse:{name:dot.animationName,duration:dot.animationDuration,repeat:dot.animationIterationCount},static:staticDot.animationName,selected};
  });
  const normal=await styles();
  expect(normal.pulse).toEqual({name:'sy-pulse',duration:'1.4s',repeat:'infinite'});expect(normal.static).toBe('none');
  expect(normal.selected).toEqual([{opacity:'0',property:'opacity',duration:'0.18s'},{opacity:'0',property:'opacity',duration:'0.18s'}]);
  await tab.evaluate(node=>node.setAttribute('aria-pressed','true'));await row.evaluate(node=>node.dataset.selected='true');
  await expect.poll(async()=>{const measured=await styles();return measured.selected.map(style=>style.opacity);}).toEqual(['1','1']);
  await tab.focus();expect(await tab.evaluate(node=>getComputedStyle(node).outlineWidth)).toBe('3px');
  await expect(fixture.getByText('回复完成 · 未保存',{exact:true})).toBeVisible();
  expect(await fixture.locator('.sy-tone-error').evaluate(node=>getComputedStyle(node).color)).toBe('rgb(255, 143, 155)');
  await page.emulateMedia({reducedMotion:'reduce'});
  const reduced=await styles();expect(reduced.pulse.name).toBe('none');expect(reduced.static).toBe('none');
  expect(reduced.selected.map(style=>({opacity:style.opacity,duration:style.duration}))).toEqual([{opacity:'1',duration:'0s'},{opacity:'1',duration:'0s'}]);
  expect(await fixture.evaluate(host=>host.getAnimations({subtree:true}).length)).toBe(0);
  await expect(fixture.getByText('已连接（测试事实）',{exact:true})).toBeVisible();
});
