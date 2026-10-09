import {test,expect} from '@playwright/test';
import {memoryServer} from './memory-server.js';
import {writeFile} from 'node:fs/promises';

test('S07 four viewports, shell and measured central boundaries retain full text and keyboard controls',async({browser},info)=>{
  test.setTimeout(45000);
  const server=await memoryServer({script:'tests/browser/tools_server.py'}),observations=[];
  try {
    for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
      const context=await browser.newContext({viewport:{width,height},hasTouch:width<1100,deviceScaleFactor:1,reducedMotion:'reduce'});
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
          fonts:[...document.querySelectorAll('.tools-page p,.tools-page button,.tools-page h3')].map(n=>Number.parseFloat(getComputedStyle(n).fontSize)),
          color:getComputedStyle(document.querySelector('.tools-page')).color,background:getComputedStyle(document.body).backgroundColor,
          transitions:[...document.querySelectorAll('.tools-page *')].filter(n=>getComputedStyle(n).transitionDuration!=='0s').length,
        }));
        expect(metrics.bodyOverflow).toBeLessThanOrEqual(0);expect(Math.min(...metrics.fonts)).toBeGreaterThanOrEqual(12);
        expect(metrics.transitions).toBe(0);expect(metrics.focusOutline).toBe('3px');expect(failed).toEqual([]);
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
