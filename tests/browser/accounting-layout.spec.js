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
      const runTable=rowRegion.getByRole('table',{name:'Run 账目索引',exact:true});
      await expect(runTable).toHaveAttribute('aria-label','Run 账目索引');
      await expect(runTable.getByRole('columnheader')).toHaveText(['Run / Session','用途与状态','归属时间','账目与过程覆盖','操作']);
      const runCells=runTable.getByRole('row').nth(1).getByRole('cell');
      await expect(runCells).toHaveCount(5);
      for(let column=0;column<5;column++)await expect(runCells.nth(column)).toHaveAttribute('headers',`ops-run-column-${column}`);
      await rowRegion.focus();
      const scroll=await rowRegion.evaluate(element=>({max:element.scrollWidth-element.clientWidth,before:element.scrollLeft}));
      expect(scroll.max).toBe(0);
      const cells=await runCells.evaluateAll(elements=>elements.map(element=>{const box=element.getBoundingClientRect();return {x:box.x,y:box.y,width:box.width,right:box.right};}));
      expect(cells[0].width).toBeGreaterThanOrEqual(200);
      expect(cells.slice(1).map(cell=>cell.width)).toEqual([100,120,170,130]);
      const centralWidth=await page.locator('.page-body').evaluate(element=>element.getBoundingClientRect().width);
      expect(cells.some(cell=>cell.y>cells[0].y)).toBe(centralWidth<816);
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
      expect(size.font).toBe('13px');expect(size.tableFont).toBe('13px');expect(size.reducedMotion).toBe(true);
      metrics.push({label:`viewport-${width}`,height,...size,cells});
      await page.screenshot({path:testInfo.outputPath(`ops-${width}-prices.png`)});
      if(width<1100){
        await page.getByRole('button',{name:'打开主对话',exact:true}).first().click();
        await expect(filter).not.toBeInViewport();await page.keyboard.press('Escape');await expect(filter).toHaveValue('未提交条件 retained');
      }
    }
    for(const target of [815,816]){
      await page.setViewportSize({width:1440,height:900});
      const actual=await page.locator('.page-body').evaluate(element=>element.getBoundingClientRect().width);
      await page.setViewportSize({width:1440+target-actual,height:900});
      const central=await page.locator('.page-body').evaluate(element=>element.getBoundingClientRect().width);
      expect(central).toBe(target);
      const cells=await page.locator('.ops-runs tbody tr').first().locator('td').evaluateAll(elements=>elements.map(element=>{const box=element.getBoundingClientRect();return {x:box.x,y:box.y,width:box.width,right:box.right};}));
      expect(cells.slice(1).map(cell=>cell.width)).toEqual([100,120,170,130]);
      expect(cells[0].width).toBe(target===816?200:343);
      if(target===816)expect(cells.map(cell=>cell.y)).toEqual(Array(5).fill(cells[0].y));
      else expect(cells[4].y).toBeGreaterThan(cells[0].y);
      metrics.push({label:`central-${target}`,central,viewport:page.viewportSize(),cells});
      await page.locator('#page').evaluate(element=>element.scrollTop=0);
      await page.screenshot({path:testInfo.outputPath(`ops-central-${target}.png`)});
    }
    await writeFile(testInfo.outputPath('layout-metrics.json'),JSON.stringify(metrics,null,2));
  }finally{await server.close();}
});
