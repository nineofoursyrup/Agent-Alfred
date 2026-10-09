import {test,expect} from '@playwright/test';
import {writeFile} from 'node:fs/promises';
import {memoryServer} from './memory-server.js';

test('S09 real Ops layout keeps filter, price and history controls reachable at reference and boundary widths',async({page},testInfo)=>{
  test.setTimeout(45000);
  const server=await memoryServer({script:'tests/browser/ops_migration_server.py'});
  const metrics=[];
  try{
    await server.send('mixed');
    await page.emulateMedia({reducedMotion:'reduce'});
    await page.goto(server.origin+'/ops?range=all&timezone=UTC');
    await page.getByRole('button',{name:/查看账目 /}).click();
    const filter=page.getByLabel('工具身份',{exact:true});
    await filter.fill('未提交条件 retained');
    const nodeIdentity=await filter.elementHandle();
    for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800],[1099,800],[1100,800]]){
      await page.setViewportSize({width,height});
      await expect(filter).toHaveValue('未提交条件 retained');
      expect(await nodeIdentity.evaluate(node=>node.isConnected)).toBe(true);
      await filter.focus();await page.keyboard.press('Tab');
      await expect(page.getByLabel('Run ID',{exact:true})).toBeFocused();
      await page.keyboard.press('Tab');await expect(page.getByRole('button',{name:'刷新账目',exact:true})).toBeFocused();
      await page.locator('#page').evaluate(element=>element.scrollTop=0);
      await page.screenshot({path:testInfo.outputPath(`ops-${width}-filters.png`)});
      const rowRegion=page.getByRole('region',{name:'Run 账目索引',exact:true});
      await rowRegion.focus();
      const scroll=await rowRegion.evaluate(element=>({max:element.scrollWidth-element.clientWidth,before:element.scrollLeft}));
      if(scroll.max>0){await page.keyboard.press('ArrowRight');await expect.poll(()=>rowRegion.evaluate(element=>element.scrollLeft)).toBeGreaterThan(scroll.before);}
      const summary=page.getByText('Token 与四维价格：four-dim-aborted',{exact:true});
      await summary.focus();
      if(!(await summary.evaluate(element=>element.parentElement.open)))await page.keyboard.press('Enter');
      const prices=page.getByRole('table',{name:'四维价格 four-dim-aborted'});
      await expect(prices).toBeVisible();
      await prices.scrollIntoViewIfNeeded();
      const size=await page.evaluate(()=>{
        const doc=document.documentElement,panel=document.querySelector('#page'),body=document.querySelector('.page-body');
        return {viewport:innerWidth,documentClient:doc.clientWidth,documentScroll:doc.scrollWidth,pageClient:panel.clientWidth,pageScroll:panel.scrollWidth,central:body.getBoundingClientRect().width,font:getComputedStyle(body).fontSize,tableFont:getComputedStyle(body.querySelector('table')).fontSize,reducedMotion:matchMedia('(prefers-reduced-motion:reduce)').matches};
      });
      expect(size.documentScroll).toBeLessThanOrEqual(size.documentClient+1);
      expect(size.pageScroll).toBeLessThanOrEqual(size.pageClient+1);
      expect(size.font).toBe('14px');expect(size.tableFont).toBe('13px');expect(size.reducedMotion).toBe(true);
      metrics.push({label:`viewport-${width}`,height,...size});
      await page.screenshot({path:testInfo.outputPath(`ops-${width}-prices.png`)});
      if(width<1100){
        await page.getByRole('button',{name:'打开主对话',exact:true}).first().click();
        await expect(filter).not.toBeInViewport();await page.keyboard.press('Escape');await expect(filter).toHaveValue('未提交条件 retained');
      }
    }
    for(const target of [679,680]){
      await page.setViewportSize({width:1440,height:900});
      const actual=await page.locator('.page-body').evaluate(element=>element.getBoundingClientRect().width);
      await page.setViewportSize({width:1440+target-actual,height:900});
      const central=await page.locator('.page-body').evaluate(element=>element.getBoundingClientRect().width);
      expect(central).toBe(target);metrics.push({label:`central-${target}`,central,viewport:page.viewportSize()});
      await page.locator('#page').evaluate(element=>element.scrollTop=0);
      await page.screenshot({path:testInfo.outputPath(`ops-central-${target}.png`)});
    }
    await writeFile(testInfo.outputPath('layout-metrics.json'),JSON.stringify(metrics,null,2));
  }finally{await server.close();}
});
