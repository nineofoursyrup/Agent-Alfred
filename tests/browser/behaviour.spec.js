import {test,expect} from '@playwright/test';
import {memoryServer} from './memory-server.js';
import {writeFile} from 'node:fs/promises';

for (const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
  test(`S06 CE-28: Behaviour controls and long drafts remain readable at ${width}x${height}`,async({page},testInfo)=>{
    const server = await memoryServer({script:'tests/browser/routing_server.py'});
    const writes=[];page.on('request',request=>{if(request.method()==='POST')writes.push(request.url());});
    try {
      await page.setViewportSize({width,height});await page.emulateMedia({reducedMotion:'reduce'});
      await page.goto(server.origin+'/behaviour');
      await expect(page.getByRole('checkbox',{name:'启用消息分流'})).toBeEnabled();
      await expect(page.getByText(/本次读取 0 个会话/)).toBeVisible();
      await page.getByRole('checkbox',{name:'启用消息分流'}).check();
      const goal=page.getByRole('textbox',{name:'聚合目标',exact:true});
      const draft='完整保留的中文目标与同名前缀标识'.repeat(18)+' source-A / source-B';await goal.fill(draft);
      const routing=page.getByRole('region',{name:'消息分流',exact:true});
      await routing.scrollIntoViewIfNeeded();await page.screenshot({path:testInfo.outputPath('initial-dirty.png')});
      await routing.getByRole('button',{name:'查看流程',exact:true}).click();
      await expect(routing.locator('svg [data-node-id]')).toHaveCount(9);
      await routing.getByRole('button',{name:'判断消息类型 · classify',exact:true}).focus();await page.keyboard.press('Enter');
      await expect(routing.locator('[aria-label="节点详情"]').getByText('节点原始定义',{exact:true})).toBeVisible();
      await routing.locator('svg').scrollIntoViewIfNeeded();await page.screenshot({path:testInfo.outputPath('graph.png')});
      await routing.getByRole('button',{name:'收起流程',exact:true}).click();
      await routing.locator('.routing-statistics > summary').focus();await page.keyboard.press('Enter');
      await expect(routing.getByText('此范围内没有已准入的聊天 Run。')).toBeVisible();
      await expect(goal).toHaveValue(draft);
      await page.getByRole('region',{name:'手动聚合',exact:true}).scrollIntoViewIfNeeded();await page.screenshot({path:testInfo.outputPath('aggregation-dirty.png')});
      const geometry=await page.evaluate(()=>({viewport:innerWidth,dpr:devicePixelRatio,central:document.querySelector('.page-body').getBoundingClientRect().width,
        body:[document.documentElement.clientWidth,document.documentElement.scrollWidth],page:[document.querySelector('#page').clientWidth,document.querySelector('#page').scrollWidth],
        font:getComputedStyle(document.querySelector('.behaviour-page')).fontFamily}));
      expect(geometry.body[1]).toBeLessThanOrEqual(geometry.body[0]);expect(geometry.page[1]).toBeLessThanOrEqual(geometry.page[0]);expect(writes).toEqual([]);
      await testInfo.attach('geometry',{body:JSON.stringify(geometry,null,2),contentType:'application/json'});
      await writeFile(testInfo.outputPath('geometry.json'),JSON.stringify(geometry,null,2));
    } finally {await server.close();}
  });
}

test('S06 CE-27/28: actual central 679/680 and shell 1099/1100 retain draft ownership',async({page},testInfo)=>{
  const server=await memoryServer({script:'tests/browser/routing_server.py'});
  try {
    await page.goto(server.origin+'/behaviour');const choice=page.getByRole('checkbox',{name:'启用消息分流'});await choice.check();
    const geometry=[];
    for(const desired of [679,680]){
      await page.setViewportSize({width:1440,height:900});
      for(let i=0;i<3;i++){
        const actual=await page.locator('.page-body').evaluate(el=>el.getBoundingClientRect().width);
        if(Math.abs(actual-desired)<1)break;
        await page.setViewportSize({width:Math.round(page.viewportSize().width+desired-actual),height:900});
      }
      const actual=await page.locator('.page-body').evaluate(el=>el.getBoundingClientRect().width);expect(Math.abs(actual-desired)).toBeLessThan(1);
      geometry.push({desired,actual,viewport:page.viewportSize()});await expect(choice).toBeChecked();await page.screenshot({path:testInfo.outputPath(`central-${desired}.png`)});
    }
    for(const width of [1099,1100]){await page.setViewportSize({width,height:800});await expect(choice).toBeChecked();await page.screenshot({path:testInfo.outputPath(`shell-${width}.png`)});}
    await page.setViewportSize({width:1440,height:900});await page.getByRole('link',{name:'运行',exact:true}).click();
    await page.getByRole('button',{name:'留在此页',exact:true}).click();await expect(page).toHaveURL(/\/behaviour$/);await expect(choice).toBeChecked();
    await page.getByRole('link',{name:'运行',exact:true}).click();await page.getByRole('button',{name:'放弃并离开',exact:true}).click();await expect(page).toHaveURL(/\/runs$/);
    await testInfo.attach('geometry',{body:JSON.stringify(geometry,null,2),contentType:'application/json'});
    await writeFile(testInfo.outputPath('geometry.json'),JSON.stringify(geometry,null,2));
  }finally{await server.close();}
});

test('S06 CE-27/28: Chromium touch simulation preserves Behaviour beneath MainBar',async({browser},testInfo)=>{
  const server=await memoryServer({script:'tests/browser/routing_server.py'});
  const context=await browser.newContext({viewport:{width:390,height:844},deviceScaleFactor:2,isMobile:true,hasTouch:true});
  const page=await context.newPage();const writes=[];page.on('request',request=>{if(request.method()==='POST')writes.push(request.url());});
  try {
    await page.goto(server.origin+'/behaviour');const choice=page.getByRole('checkbox',{name:'启用消息分流'});await choice.tap();
    const goal=page.getByRole('textbox',{name:'聚合目标',exact:true});await goal.fill('触控模拟下保留的目标');
    const routing=page.getByRole('region',{name:'消息分流',exact:true});await routing.getByRole('button',{name:'查看流程',exact:true}).tap();
    await expect(routing.locator('svg [data-node-id]')).toHaveCount(9);
    await routing.getByRole('button',{name:'放大',exact:true}).tap();
    await page.locator('#shell-toolbar [data-open-panel="mainbar"]').tap();
    await expect(goal).toBeHidden();await page.getByRole('button',{name:'收起主对话',exact:true}).tap();
    await expect(choice).toBeChecked();await expect(goal).toHaveValue('触控模拟下保留的目标');
    await page.setViewportSize({width:320,height:800});await goal.scrollIntoViewIfNeeded();
    await expect(goal).toHaveValue('触控模拟下保留的目标');expect(writes).toEqual([]);
    await page.screenshot({path:testInfo.outputPath('touch-simulation.png')});
    await writeFile(testInfo.outputPath('simulation.json'),JSON.stringify({scope:'Chromium viewport/touch simulation only; no real iPhone, iOS Safari or mobile keyboard',viewport:page.viewportSize(),dpr:await page.evaluate(()=>devicePixelRatio)},null,2));
  } finally {await context.close();await server.close();}
});
