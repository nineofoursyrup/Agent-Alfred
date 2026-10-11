import {chromium,expect} from '@playwright/test';
import {memoryServer} from '../../tests/browser/memory-server.js';
import {writeFile,readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
const evidence=process.env.V04_CAPTURE_DIR||'docs/implementation/dashboard-claude-design/evidence/V04';
const browser=await chromium.launch();
const server=await memoryServer({script:'tests/browser/overview_server.py'});
const files=['src/agent_alfred/ops/static/overview.js','src/agent_alfred/ops/static/overview.css','tests/browser/overview.spec.js'];
const sourceHashes=Object.fromEntries(await Promise.all(files.map(async file=>[file,createHash('sha256').update(await readFile(file)).digest('hex')])));
const records={tested_files_sha256:sourceHashes,execution:'before_commit; tested file SHA-256 recorded',costFacts:[],port:process.env.ALFRED_BROWSER_TEST_PORT,origin:server.origin,browser:browser.version(),captured_at:new Date().toISOString(),viewports:[],console:[],pageErrors:[]};
try {
  await server.send('mode mixed');await server.send('runs 1');await server.send('mode exact');await server.send('runs 4');await server.send('memory 12');
  const page=await browser.newPage();
  page.on('console',msg=>{if(msg.type()==='error')records.console.push(msg.text());});
  page.on('pageerror',error=>records.pageErrors.push(error.message));
  const initialPeriod=page.waitForResponse('**/api/overview/period?**');
  await page.goto(server.origin+'/overview?range=7d&timezone=Asia%2FShanghai');
  const initialWire=await (await initialPeriod).json();
  await expect(page.getByLabel('期间指标')).toContainText('5 条 Run 记录');
  await expect(page.getByLabel('期间指标')).toContainText('精确 USD 1.75');
  await expect(page.getByLabel('期间指标')).toContainText('估算 USD 0.003');
  await expect(page.getByLabel('当前记忆条目数')).toContainText('语义 12 条');
  const cost=page.locator('.overview-period-cards article').nth(1);
  async function inspectCost(state,wire) {
    const s=wire.summary,u=wire.unresolved_membership_count;
    await expect(cost).toContainText(`已记录 Attempt ${s.attempt_count} 次 · 费用未知 ${s.unknown_cost_attempts} 次`);
    await expect(cost).toContainText(wire.model_coverage.complete?'模型账目完整':`模型账目覆盖不足 ${wire.model_coverage.incomplete_runs} 条 Run：`);
    if(u)await expect(cost).toContainText(`另 ${u} 条期间无法判定`);
    const evidence=await cost.evaluate(node=>({dom:[...node.children].map(child=>({tag:child.tagName,text:child.textContent,y:child.getBoundingClientRect().y})),snapshot:node.textContent}));
    expect(evidence.dom[0].tag).toBe('H3');
    for(const child of evidence.dom.slice(1))expect(child.y).toBeLessThan(evidence.dom[0].y);
    const periodEvidence=page.locator('.overview-period .overview-source-detail');
    expect(await periodEvidence.locator(':scope > p').allTextContents()).not.toContain(`已记录 Attempt ${s.attempt_count} 次 · 费用未知 ${s.unknown_cost_attempts} 次`);
    await expect(periodEvidence).not.toContainText('模型账目完整');
    await expect(periodEvidence).not.toContainText('模型账目覆盖不足');
    records.costFacts.push({state,wire,evidence,accessibility:await cost.ariaSnapshot()});
  }
  await inspectCost('mixed',initialWire);expect(initialWire.summary.unknown_cost_attempts).toBeGreaterThan(0);
  records.initialInlineStyleAttributes=await page.locator('.overview [style]').count();expect(records.initialInlineStyleAttributes).toBe(0);
  const diagram=page.getByRole('region',{name:'静态架构说明图示（宽度不足时可横向滚动）',exact:true});
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
    if(width===1440)expect(await diagram.evaluate(node=>node.scrollLeft)).toBe(0);
    else await expect.poll(()=>diagram.evaluate(node=>node.scrollLeft)).toBeGreaterThan(0);
    const scroll=await diagram.evaluate(node=>({scrollLeft:node.scrollLeft,clientWidth:node.clientWidth,scrollWidth:node.scrollWidth,outline:getComputedStyle(node).outlineWidth}));
    await diagram.evaluate(node=>node.scrollLeft=0);
    const geometry=await page.evaluate(()=>({body:document.body.scrollWidth,viewport:innerWidth,page:document.querySelector('#page').clientWidth,pageScroll:document.querySelector('#page').scrollWidth,nodeTitleFont:getComputedStyle(document.querySelector('.overview-diagram-node text')).fontSize,animation:document.querySelector('.overview-diagram').getAnimations({subtree:true}).length,inlineStyleAttributes:document.querySelector('.overview').querySelectorAll('[style]').length,inlineStyles:[...document.querySelector('.overview').querySelectorAll('[style]')].filter(node=>node.getAttribute('style').trim()).length,cards:[...document.querySelectorAll('.overview-metrics .sy-metric-card')].map(node=>({width:node.getBoundingClientRect().width,valueFont:getComputedStyle(node.querySelector('.sy-metric-value')).fontFamily})),statuses:[...document.querySelectorAll('.overview-run-states span')].map(node=>({text:node.textContent,client:node.clientWidth,scroll:node.scrollWidth,overflow:getComputedStyle(node).textOverflow})),costColors:[...document.querySelectorAll('.overview-cost-values .sy-metric-value')].map(node=>getComputedStyle(node).color),success:getComputedStyle(document.documentElement).getPropertyValue('--success').trim()}));
    records.current={width,height,scroll,geometry,inlineStyles:await page.locator('.overview [style]').evaluateAll(nodes=>nodes.map(node=>node.outerHTML))};expect(geometry.body).toBeLessThanOrEqual(width);expect(geometry.pageScroll).toBeLessThanOrEqual(geometry.page+1);expect(geometry.nodeTitleFont).toBe('13px');expect(geometry.animation).toBe(0);expect(geometry.inlineStyles).toBe(0);expect(scroll.outline).toBe('3px');
    const svg=await diagram.getByRole('img').evaluate(node=>{
      const texts=[...node.querySelectorAll('text')].map(text=>({text:text.textContent,...Object.fromEntries(['x','y','width','height'].map(key=>[key,text.getBBox()[key]]))}));
      const nodes=[...node.querySelectorAll('.overview-diagram-node rect')].map(rect=>Object.fromEntries(['x','y','width','height'].map(key=>[key,rect.getBBox()[key]])));
      const overlaps=[];const borders=[];const outside=[];
      for(let i=0;i<texts.length;i++){
        const a=texts[i];
        if(a.x<0||a.y<0||a.x+a.width>node.viewBox.baseVal.width||a.y+a.height>node.viewBox.baseVal.height)outside.push(a.text);
        for(let j=i+1;j<texts.length;j++){const b=texts[j];if(a.x<b.x+b.width&&a.x+a.width>b.x&&a.y<b.y+b.height&&a.y+a.height>b.y)overlaps.push([a.text,b.text]);}
        for(const b of nodes){
          const horizontal=(a.y<b.y&&a.y+a.height>b.y||a.y<b.y+b.height&&a.y+a.height>b.y+b.height)&&a.x<b.x+b.width&&a.x+a.width>b.x;
          const vertical=(a.x<b.x&&a.x+a.width>b.x||a.x<b.x+b.width&&a.x+a.width>b.x+b.width)&&a.y<b.y+b.height&&a.y+a.height>b.y;
          if(horizontal||vertical)borders.push(a.text);
        }
      }
      const labels=[...node.querySelectorAll('.overview-edge-label')];
      const edges=[...node.querySelectorAll('.overview-edge')].map((edge,index)=>{
        const segments=[];let x=0,y=0;
        for(const part of edge.getAttribute('d').match(/[MHV][^MHV]+/g)){
          const numbers=part.slice(1).trim().split(/\s+/).map(Number);
          if(part[0]==='M'){[x,y]=numbers;continue;}
          const nx=part[0]==='H'?numbers[0]:x,ny=part[0]==='V'?numbers[0]:y;segments.push({x,y,nx,ny});x=nx;y=ny;
        }
        const label=labels[index].getBBox();
        const distance=Math.min(...segments.map(s=>Math.hypot(Math.max(Math.min(s.x,s.nx)-label.x-label.width,label.x-Math.max(s.x,s.nx),0),Math.max(Math.min(s.y,s.ny)-label.y-label.height,label.y-Math.max(s.y,s.ny),0))));
        const alongBorders=segments.filter(s=>nodes.some(b=>s.y===s.ny&&(s.y===b.y||s.y===b.y+b.height)&&Math.min(Math.max(s.x,s.nx),b.x+b.width)>Math.max(Math.min(s.x,s.nx),b.x)||s.x===s.nx&&(s.x===b.x||s.x===b.x+b.width)&&Math.min(Math.max(s.y,s.ny),b.y+b.height)>Math.max(Math.min(s.y,s.ny),b.y)));
        const marker=getComputedStyle(edge).markerEnd;
        const reference=new URL(marker.replace(/^url\(["']?|["']?\)$/g,''),node.baseURI);
        return {path:edge.getAttribute('d'),label:labels[index].textContent,distance,alongBorders,optional:edge.classList.contains('overview-edge-optional'),markerAttribute:edge.getAttribute('marker-end'),computedMarker:marker,markerSameDocument:reference.href.split('#')[0]===node.baseURI.split('#')[0],markerInSvg:node.querySelector(reference.hash)?.tagName==='marker'};
      });
      return {viewBoxWidth:node.viewBox.baseVal.width,renderedWidth:node.getBoundingClientRect().width,texts,nodes,overlaps,borders,outside,edges,partitions:node.querySelectorAll('.overview-partition').length};
    });
    records.current.svg=svg;
    expect(svg.viewBoxWidth).toBe(780);expect(svg.renderedWidth).toBe(svg.viewBoxWidth);
    expect(svg.nodes).toHaveLength(6);expect(svg.edges).toHaveLength(6);expect(svg.partitions).toBe(3);
    expect(svg.overlaps).toEqual([]);expect(svg.borders).toEqual([]);expect(svg.outside).toEqual([]);
    expect(svg.edges.filter(edge=>edge.optional)).toHaveLength(3);
    for(const edge of svg.edges){expect(edge.distance).toBeLessThanOrEqual(12);expect(edge.alongBorders).toEqual([]);expect(edge.label).not.toContain('→');expect(edge.markerAttribute).toBe('url(#overview-arrow)');expect(edge.computedMarker).toContain('#overview-arrow');expect(edge.markerSameDocument).toBe(true);expect(edge.markerInSvg).toBe(true);}
    if(width===1440)expect(scroll.scrollWidth).toBeLessThanOrEqual(scroll.clientWidth);
    for(const state of geometry.statuses){expect(state.overflow).not.toBe('ellipsis');expect(state.scroll).toBeLessThanOrEqual(state.client);}
    await page.getByRole('heading',{name:'全历史最近运行',exact:true}).evaluate(node=>node.scrollIntoView({block:'start'}));
    await page.screenshot({path:`${evidence}/recent-${width}x${height}.png`});
    records.viewports.push({width,height,scroll,geometry,svg});
    await writeFile(`${evidence}/overview-${width}x${height}-snapshot.txt`,await page.locator('#page').ariaSnapshot());
  }
  await server.send('opaque-runs');await server.send('telemetry interrupted');await page.getByRole('button',{name:'刷新最近运行',exact:true}).click();
  await expect(page.getByLabel('全历史最近运行')).toContainText('终态无法确认');await expect(page.getByLabel('全历史最近运行')).toContainText('记录状态未知');
  await page.getByRole('heading',{name:'全历史最近运行',exact:true}).evaluate(node=>node.scrollIntoView({block:'start'}));
  await page.locator('.overview-run-preview').filter({hasText:'未知用途：future-purpose'}).evaluate(node=>node.scrollIntoView({block:'center'}));
  await page.screenshot({path:`${evidence}/long-identities-unknown-states-320x800.png`});
  records.stress=await page.locator('.overview-run').evaluateAll(rows=>rows.map(node=>({identity:node.querySelector('a').textContent,preview:node.querySelector('.overview-run-preview').textContent,previewOverflow:getComputedStyle(node.querySelector('.overview-run-preview')).textOverflow,previewWhiteSpace:getComputedStyle(node.querySelector('.overview-run-preview')).whiteSpace,previewOverflowX:getComputedStyle(node.querySelector('.overview-run-preview')).overflowX,states:[...node.querySelectorAll('.overview-run-states span')].map(state=>({text:state.textContent,width:state.getBoundingClientRect().width,overflow:getComputedStyle(state).textOverflow})),scroll:node.scrollWidth,width:node.clientWidth})));
  for(const row of records.stress) {expect(row.scroll).toBeLessThanOrEqual(row.width+1);expect(row.previewOverflow).toBe('clip');expect(row.previewWhiteSpace).toBe('normal');expect(row.previewOverflowX).toBe('visible');for(const state of row.states)expect(state.overflow).not.toBe('ellipsis');}
  const unknown=page.locator('.overview-run-preview').filter({hasText:'未知用途：future-purpose'});
  await expect(unknown).toHaveText('未知用途：future-purpose');
  records.unknownPurpose=await unknown.evaluate(node=>{const range=document.createRange();range.selectNodeContents(node);return {text:node.textContent,lines:range.getClientRects().length,height:node.clientHeight,scrollHeight:node.scrollHeight,width:node.clientWidth,scrollWidth:node.scrollWidth,overflow:getComputedStyle(node).overflow,whiteSpace:getComputedStyle(node).whiteSpace};});
  expect(records.unknownPurpose.lines).toBeGreaterThan(1);expect(records.unknownPurpose.scrollHeight).toBeLessThanOrEqual(records.unknownPurpose.height);expect(records.unknownPurpose.scrollWidth).toBeLessThanOrEqual(records.unknownPurpose.width);
  async function refreshCost(state) {
    const response=page.waitForResponse('**/api/overview/period?**');await page.getByRole('button',{name:'刷新期间指标',exact:true}).click();const wire=await (await response).json();await inspectCost(state,wire);return wire;
  }
  const incomplete=await refreshCost('interrupted');expect(incomplete.model_coverage.complete).toBe(false);expect(incomplete.model_coverage.incomplete_runs).toBeGreaterThan(0);
  await server.send('telemetry bad-time');const unresolved=await refreshCost('bad-time');expect(unresolved.unresolved_membership_count).toBeGreaterThan(0);
  await expect(page.locator('.overview-period-cards article').first().locator('p')).toContainText('— 已定位 0 条；另 5 条期间无法判定');
  await server.send('mode exact');await server.send('runs 1');const partial=await refreshCost('known-and-unresolved');expect(partial.summary.run_count).toBe(1);expect(partial.unresolved_membership_count).toBe(5);
  await expect(page.locator('.overview-period-cards article').first().locator('p')).toContainText('— 已定位 1 条；另 5 条期间无法判定');
  expect(records.pageErrors).toEqual([]);
  records.exit_code=0;
} catch(error) {records.exit_code=1;records.error=error.stack;throw error;}
finally {
  records.candidate_sha256=Object.fromEntries(await Promise.all(files.map(async file=>[file,createHash('sha256').update(await readFile(file)).digest('hex')])));
  records.tested_files_unchanged=JSON.stringify(sourceHashes)===JSON.stringify(records.candidate_sha256);
  await writeFile(`${evidence}/screenshots-audit.json`,JSON.stringify(records,null,2)+'\n');await browser.close();await server.close();
}
