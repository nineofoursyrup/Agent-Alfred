import {chromium,expect} from '@playwright/test';
import {memoryServer,api} from '../../tests/browser/memory-server.js';
import {mkdir,writeFile,readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
process.env.ALFRED_BROWSER_TEST_PORT='18100';
const folder='.scratch/v10/product';
await mkdir(folder,{recursive:true});
const server=await memoryServer();
const browser=await chromium.launch();
const seedRequest=await browser.newContext();
const client=await api(seedRequest.request,server.origin);
for(const [index,fact] of ['只读诊断结果保留读取时刻与保护版本。','手写 SQL 草稿与已提交查询分别归属。','NULL 与空文本使用不同的可辨表达。'].entries()){
 const saved=await client.command({operation_id:'v10-capture-'+index,kind:'semantic',action:'save',payload:{subject:'V10 本地视觉检查',fact}});
 expect(saved.status).toBe(200);
}
const report={captured_at:new Date().toISOString(),browser:browser.version(),origin:server.origin,fixture:'existing memory_server.py, real Host/HTTP/SQLite, temporary state, offline model',viewports:[],source_sha256:{},page_errors:[],request_failures:[],external_requests:[]};
for(const path of ['src/agent_alfred/ops/static/database.js','src/agent_alfred/ops/static/database.css','tests/browser/database_migration.spec.js'])report.source_sha256[path]=createHash('sha256').update(await readFile(path)).digest('hex');
try{
 for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]){
  const context=await browser.newContext({viewport:{width,height},hasTouch:width<1100,deviceScaleFactor:1,reducedMotion:'reduce'});
  const page=await context.newPage();
  let closing=false;
  page.on('pageerror',e=>report.page_errors.push({width,error:e.message}));
  page.on('requestfailed',r=>report.request_failures.push({width,url:r.url(),error:r.failure()?.errorText,phase:closing?'context-close':'active'}));
  page.on('request',r=>{if(!r.url().startsWith(server.origin))report.external_requests.push(r.url());});
  await page.goto(server.origin+'/database');
  await expect(page.getByText('可执行',{exact:true})).toBeVisible();
  const editor=page.getByRole('textbox',{name:'SQL',exact:true});
  const run=page.getByRole('button',{name:'执行',exact:true});
  const result=page.getByRole('region',{name:'查询结果',exact:true});
  const submitted=page.getByRole('region',{name:'已提交查询',exact:true});
  await expect(submitted).toBeEmpty();
  await editor.fill('SELECT subject, fact FROM diag_facts LIMIT 20');
  await run.click();
  await expect(result.locator('td')).toHaveCount(6);
  await expect(page.getByText('清理已释放',{exact:true})).toBeVisible();
  await page.locator('#page').evaluate(el=>el.scrollTop=0);
  await page.mouse.move(0,0);
  await page.screenshot({path:`${folder}/database-${width}x${height}.png`});
  await writeFile(`${folder}/database-${width}x${height}-snapshot.txt`,await page.locator('body').ariaSnapshot());
  const item=await page.evaluate(()=>{
   const style=selector=>{const el=document.querySelector(selector);const s=getComputedStyle(el);const box=el.getBoundingClientRect();return {width:box.width,height:box.height,fontSize:s.fontSize,fontFamily:s.fontFamily,fontStyle:s.fontStyle,color:s.color,background:s.backgroundColor,borderRadius:s.borderRadius,padding:s.padding,borderColor:s.borderColor,gridTemplateColumns:s.gridTemplateColumns};};
   return {width:innerWidth,height:innerHeight,dpr:devicePixelRatio,touch:navigator.maxTouchPoints,central:style('.db-console'),console:style('.db-query-panel'),editor:style('#database-sql'),title:style('.db-query-panel h2'),facts:style('.db-facts'),header:style('.db-result th'),run:style('.db-actions .sy-button-accent'),page_overflow:document.querySelector('#page').scrollWidth-document.querySelector('#page').clientWidth,document_overflow:document.documentElement.scrollWidth-innerWidth,inline_style_count:[...document.querySelectorAll('[style]')].filter(el=>el.style.length>0).length};
  });
  item.inline_style_elements=await page.locator('[style]').evaluateAll(els=>els.map(el=>({tag:el.tagName,id:el.id,class:el.className,style:el.getAttribute('style')})));
  expect(item.page_overflow).toBe(0); expect(item.document_overflow).toBe(0); expect(item.inline_style_count).toBe(0);
  const scroll=page.getByRole('region',{name:'数据表横向滚动区',exact:true});
  await scroll.scrollIntoViewIfNeeded();
  await page.screenshot({path:`${folder}/database-result-${width}x${height}.png`});
  const long='长值完整保留 '.repeat(80)+'LAST-VALUE';
  await editor.fill(`SELECT NULL AS null_value, '' AS empty_value, 'NULL' AS literal_value, 1 AS integer_value, 1.0 AS real_value, X'01ff' AS blob_value, '${long}' AS long_value`);
  await run.click();
  await expect(result.locator('td')).toHaveText(['NULL','""','"NULL"','1','1.0','BLOB 01ff (2 字节)',JSON.stringify(long)]);
  await scroll.scrollIntoViewIfNeeded(); await scroll.focus();
  item.table_focus_outline=await scroll.evaluate(el=>getComputedStyle(el).outlineWidth);
  expect(item.table_focus_outline).toBe('3px');
  await page.keyboard.press('ArrowRight');
  await expect.poll(()=>scroll.evaluate(el=>el.scrollLeft)).toBeGreaterThan(0);
  await page.screenshot({path:`${folder}/database-long-result-${width}x${height}.png`});
  item.long_result=await page.evaluate(()=>({page_overflow:document.querySelector('#page').scrollWidth-document.querySelector('#page').clientWidth,document_overflow:document.documentElement.scrollWidth-innerWidth,null_font_style:getComputedStyle(document.querySelector('.sql-null')).fontStyle,table_columns:document.querySelectorAll('.db-result th').length,scroll_left:document.querySelector('.table-scroll').scrollLeft,cell_text:[...document.querySelectorAll('.db-result td')].map(el=>el.textContent)}));
  expect(item.long_result.page_overflow).toBe(0);expect(item.long_result.document_overflow).toBe(0);expect(item.long_result.null_font_style).toBe('italic');
  report.viewports.push(item);
  closing=true;
  await context.close();
 }
 expect(report.page_errors).toEqual([]);expect(report.external_requests).toEqual([]);
 report.exit_code=0;
}catch(error){report.exit_code=1;report.error=error.stack;throw error;}
finally{await writeFile(folder+'/capture.json',JSON.stringify(report,null,2)+'\n');await seedRequest.close();await browser.close();await server.close();}
console.log(JSON.stringify({exit_code:report.exit_code,viewports:report.viewports.map(v=>({width:v.width,height:v.height,central:v.central.width,overflow:v.document_overflow})),page_errors:report.page_errors,external_requests:report.external_requests}));
