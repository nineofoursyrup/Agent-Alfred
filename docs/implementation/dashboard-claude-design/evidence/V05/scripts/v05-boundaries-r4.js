async (page) => {
 await page.setViewportSize({width:1440,height:900});
 const target='0fea87cbb46c45b1afce86d9a1b1a9bc', base='docs/implementation/dashboard-claude-design/evidence/V05';
 const reads=[],consoleErrors=[];page.on('request',r=>{if(r.method()==='GET') reads.push(new URL(r.url()).pathname)});page.on('console',m=>{if(m.type()==='error') consoleErrors.push(m.text())});
 let first=true;
 await page.route('**/api/runs/locate/*',async route=>{if(first){first=false;await route.abort();}else await route.continue();});
 await page.goto(`http://127.0.0.1:17920/runs/${target}`);
 await page.getByRole('status').filter({hasText:'已读摘要保留'}).waitFor();
 const summaryButton=page.getByRole('button',{name:'刷新运行摘要',exact:true}),evidenceButton=page.getByRole('button',{name:'刷新过程证据',exact:true});
 const firstFailure={summaryRetryVisible:await summaryButton.isVisible(),evidenceRetryVisible:await evidenceButton.isVisible(),snapshot:await page.getByRole('main').ariaSnapshot()};
 await page.screenshot({path:`${base}/initial-read-failure-1440x900.png`});
 await summaryButton.click();
 await page.getByRole('region',{name:'运行过程',exact:true}).getByText('过程记录可用 · available',{exact:true}).waitFor();
 await page.unroute('**/api/runs/locate/*');
 const initialRecover=await page.getByRole('region',{name:'运行摘要',exact:true}).getByText(target,{exact:true}).isVisible();
 const event=(seq,attempt,name,payload={})=>({seq,envelope:{run_id:target,step_index:0,attempt_id:attempt,ts:seq},payload:{name,...payload}});
 const fixture={run_id:target,trace_status:'available',trace_incomplete:true,recording_state:'recorded',attempts:[{attempt_id:'void',usage:{output_tokens:3},cost:{state:'unknown',amount:'999'}},{attempt_id:'estimated',usage:{output_tokens:6},cost:{state:'estimated',amount:'0.006',price_components:[{dimension:'output',price_source:'catalog',stale:true},{dimension:'uncached_input',price_source:'model_static',tiered:true}]}},{attempt_id:'exact',usage:{output_tokens:9},cost:{state:'exact',amount:'0.125'}}],events:[event(1,'void','attempt.started'),event(2,'void','attempt.aborted',{blocks:[{type:'text',text:'已作废片段'}]}),event(3,'estimated','attempt.started'),event(4,'estimated','attempt.committed',{blocks:[{type:'text',text:'提交快照'}]}),event(5,'exact','attempt.started'),event(6,'exact','attempt.committed',{blocks:[{type:'text',text:'账目核对快照'}]})]};
 async function refreshHoldingFocus(expired) {
  let release,entered;
  const held=new Promise(resolve=>release=resolve), started=new Promise(resolve=>entered=resolve);
  await page.route('**/api/run-evidence?*',async route=>{entered();await held;await route.fulfill(expired?{status:410,json:{error:{code:'snapshot_expired'}}}:{json:fixture});});
  const response=page.waitForResponse(r=>r.url().includes('/api/run-evidence?'));
  await evidenceButton.click();await started;await summaryButton.focus();release();await response;
  await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(n=>n.textContent==='刷新过程证据'&&!n.disabled));
  const focused=await summaryButton.evaluate(n=>document.activeElement===n);
  await page.unroute('**/api/run-evidence?*');
  return focused;
 }
 const evidenceFocus=await refreshHoldingFocus(false);
 const summaryFocus=await refreshHoldingFocus(true);
 const expiredSnapshot=await page.getByRole('main').ariaSnapshot();
 await refreshHoldingFocus(false);
 const summaryText=await page.getByRole('region',{name:'运行摘要',exact:true}).innerText();
 const aborted=page.getByRole('region',{name:'运行过程',exact:true}).locator('details[data-attempt="void"]');
 await aborted.locator('summary').click();
 await page.setViewportSize({width:1440,height:900});await page.screenshot({path:`${base}/boundary-states-1440x900.png`});
 await page.setViewportSize({width:320,height:800});await page.screenshot({path:`${base}/boundary-states-320x800.png`});
 const boundary={summaryText,aborted:await aborted.innerText(),snapshot:await page.getByRole('main').ariaSnapshot(),overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)};
 await page.unroute('**/api/run-evidence?*');
 const ok=firstFailure.summaryRetryVisible&&firstFailure.evidenceRetryVisible&&initialRecover&&evidenceFocus&&summaryFocus&&summaryText.includes('unknown · 费用未知')&&summaryText.includes('exact · USD 0.125')&&summaryText.includes('estimated · USD 0.006')&&summaryText.includes('output: catalog · stale')&&summaryText.includes('uncached_input: model_static')&&summaryText.includes('按基础档估算')&&!summaryText.includes('999')&&!boundary.overflow&&reads.every(p=>p!='/api/runs');
 return {ok,firstFailure,initialRecover,evidenceFocus,summaryFocus,expiredSnapshot,boundary,reads,consoleErrors,fixture};
}
