import {chromium,expect} from '@playwright/test';
import {memoryServer} from '../../tests/browser/memory-server.js';
import {writeFile,readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
const evidence='docs/implementation/dashboard-claude-design/evidence/V04';
const browser=await chromium.launch();
const server=await memoryServer({script:'tests/browser/overview_server.py'});
const records={port:process.env.ALFRED_BROWSER_TEST_PORT,origin:server.origin,browser:browser.version(),captured_at:new Date().toISOString(),viewports:[],console:[],pageErrors:[]};
try {
  await server.send('mode mixed');await server.send('runs 1');await server.send('mode exact');await server.send('runs 4');await server.send('memory 12');
  const page=await browser.newPage();
  page.on('console',msg=>{if(msg.type()==='error')records.console.push(msg.text());});
  page.on('pageerror',error=>records.pageErrors.push(error.message));
  await page.goto(server.origin+'/overview?range=7d&timezone=Asia%2FShanghai');
  await expect(page.getByLabel('期间指标')).toContainText('5 条 Run 记录');
  await expect(page.getByLabel('期间指标')).toContainText('精确 USD 1.75');
  await expect(page.getByLabel('期间指标')).toContainText('估算 USD 0.003');
  await expect(page.getByLabel('当前记忆条目数')).toContainText('语义 12 条');
  records.initialInlineStyleAttributes=await page.locator('.overview [style]').count();expect(records.initialInlineStyleAttributes).toBe(0);
  const diagram=page.getByRole('region',{name:'静态架构说明图示（可横向滚动）',exact:true});
  await expect(diagram).toHaveAttribute('tabindex','0');
  await expect(diagram.getByRole('img')).toHaveAccessibleName('CLI 与 Web 入口提交到共享宿主。宿主连接对话处理、显式手动聚合与持久账目；对话和聚合按需使用模型、工具、记忆。');
  for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
    await page.setViewportSize({width,height});
    await expect(page.locator('.overview-run')).toHaveCount(5);
    await page.locator('#page').evaluate(node=>node.scrollTop=0);
    await page.screenshot({path:`${evidence}/overview-${width}x${height}.png`});
    await page.getByRole('heading',{name:'静态架构说明',exact:true}).evaluate(node=>node.scrollIntoView({block:'start'}));
    await page.screenshot({path:`${evidence}/architecture-${width}x${height}.png`});
    await diagram.focus();await expect(diagram).toBeFocused();
    await diagram.evaluate(node=>node.scrollLeft=0);
    await page.keyboard.press('ArrowRight');
    await expect.poll(()=>diagram.evaluate(node=>node.scrollLeft)).toBeGreaterThan(0);
    const scroll=await diagram.evaluate(node=>({scrollLeft:node.scrollLeft,clientWidth:node.clientWidth,scrollWidth:node.scrollWidth,outline:getComputedStyle(node).outlineWidth}));
    await diagram.evaluate(node=>node.scrollLeft=0);
    const geometry=await page.evaluate(()=>({body:document.body.scrollWidth,viewport:innerWidth,page:document.querySelector('#page').clientWidth,pageScroll:document.querySelector('#page').scrollWidth,nodeTitleFont:getComputedStyle(document.querySelector('.overview-diagram-node text')).fontSize,animation:document.querySelector('.overview-diagram').getAnimations({subtree:true}).length,inlineStyleAttributes:document.querySelector('.overview').querySelectorAll('[style]').length,inlineStyles:[...document.querySelector('.overview').querySelectorAll('[style]')].filter(node=>node.getAttribute('style').trim()).length,cards:[...document.querySelectorAll('.overview-metrics .sy-metric-card')].map(node=>({width:node.getBoundingClientRect().width,valueFont:getComputedStyle(node.querySelector('.sy-metric-value')).fontFamily})),statuses:[...document.querySelectorAll('.overview-run-states span')].map(node=>({text:node.textContent,client:node.clientWidth,scroll:node.scrollWidth,overflow:getComputedStyle(node).textOverflow})),costColors:[...document.querySelectorAll('.overview-cost-values .sy-metric-value')].map(node=>getComputedStyle(node).color),success:getComputedStyle(document.documentElement).getPropertyValue('--success').trim()}));
    records.current={width,height,scroll,geometry,inlineStyles:await page.locator('.overview [style]').evaluateAll(nodes=>nodes.map(node=>node.outerHTML))};expect(geometry.body).toBeLessThanOrEqual(width);expect(geometry.pageScroll).toBeLessThanOrEqual(geometry.page+1);expect(geometry.nodeTitleFont).toBe('14.5px');expect(geometry.animation).toBe(0);expect(geometry.inlineStyles).toBe(0);expect(scroll.outline).toBe('3px');
    for(const state of geometry.statuses){expect(state.overflow).not.toBe('ellipsis');expect(state.scroll).toBeLessThanOrEqual(state.client);}
    await page.getByRole('heading',{name:'全历史最近运行',exact:true}).evaluate(node=>node.scrollIntoView({block:'start'}));
    await page.screenshot({path:`${evidence}/recent-${width}x${height}.png`});
    records.viewports.push({width,height,scroll,geometry});
    await writeFile(`${evidence}/overview-${width}x${height}-snapshot.txt`,await page.locator('#page').ariaSnapshot());
  }
  await server.send('opaque-runs');await server.send('telemetry interrupted');await page.getByRole('button',{name:'刷新最近运行',exact:true}).click();
  await expect(page.getByLabel('全历史最近运行')).toContainText('终态无法确认');await expect(page.getByLabel('全历史最近运行')).toContainText('记录状态未知');
  await page.getByRole('heading',{name:'全历史最近运行',exact:true}).evaluate(node=>node.scrollIntoView({block:'start'}));
  await page.screenshot({path:`${evidence}/long-identities-unknown-states-320x800.png`});
  records.stress=await page.locator('.overview-run').evaluateAll(rows=>rows.map(node=>({identity:node.querySelector('a').textContent,preview:node.querySelector('.overview-run-preview').textContent,previewOverflow:getComputedStyle(node.querySelector('.overview-run-preview')).textOverflow,states:[...node.querySelectorAll('.overview-run-states span')].map(state=>({text:state.textContent,width:state.getBoundingClientRect().width,overflow:getComputedStyle(state).textOverflow})),scroll:node.scrollWidth,width:node.clientWidth})));
  for(const row of records.stress) {expect(row.scroll).toBeLessThanOrEqual(row.width+1);expect(row.previewOverflow).toBe('ellipsis');for(const state of row.states)expect(state.overflow).not.toBe('ellipsis');}
  expect(records.pageErrors).toEqual([]);
  records.exit_code=0;
} catch(error) {records.exit_code=1;records.error=error.stack;throw error;}
finally {
  const files=['src/agent_alfred/ops/static/overview.js','src/agent_alfred/ops/static/overview.css','tests/browser/overview.spec.js'];records.candidate_sha256=Object.fromEntries(await Promise.all(files.map(async file=>[file,createHash('sha256').update(await readFile(file)).digest('hex')])));
  await writeFile(`${evidence}/screenshots-audit.json`,JSON.stringify(records,null,2)+'\n');await browser.close();await server.close();
}
