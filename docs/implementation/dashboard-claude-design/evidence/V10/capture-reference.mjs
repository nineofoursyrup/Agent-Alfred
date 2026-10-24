import {chromium, expect} from '@playwright/test';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
const folder='.scratch/v10/reference';
const source='docs/design/issue-122/reference';
await mkdir(folder,{recursive:true});
const server=spawn('.venv/bin/python',['-u','-m','http.server','18108','--bind','127.0.0.1','--directory',source]);
const closed=once(server,'close');
let stderr=''; server.stderr.on('data',chunk=>stderr+=chunk);
await Promise.race([once(server.stdout,'data'),closed.then(()=>{throw Error(stderr);})]);
const browser=await chromium.launch();
const page=await browser.newPage({viewport:{width:1440,height:900},deviceScaleFactor:1});
const report={view:'database',version:'1786950414092924',captured_at:new Date().toISOString(),browser:browser.version(),network_dependencies:[],page_errors:[],request_failures:[],viewports:[]};
for(const filename of ['Syrup Dashboard.dc.html','support.js'])report[filename+'_sha256']=createHash('sha256').update(await readFile(source+'/'+filename)).digest('hex');
page.on('response',r=>{if(r.url().includes('unpkg.com'))report.network_dependencies.push({url:r.url(),status:r.status()});});
page.on('pageerror',e=>report.page_errors.push(e.message));
page.on('requestfailed',r=>report.request_failures.push({url:r.url(),error:r.failure()?.errorText}));
try{
 await page.goto('http://127.0.0.1:18108/Syrup%20Dashboard.dc.html');
 const link=page.getByRole('link',{name:/^Database/});
 await expect(link).toBeVisible();
 report.navigation_accessible_name=(await link.innerText()).replace(/\s+/g,' ');
 await link.click();
 await expect(page.locator('main').getByText(/^SQL CONSOLE/)).toBeVisible();
 for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]){
  await page.setViewportSize({width,height});
  await page.locator('main').evaluate(el=>el.scrollTop=0);
  await page.screenshot({path:`${folder}/reference-database-${width}x${height}.png`,animations:'disabled'});
  await writeFile(`${folder}/reference-database-${width}x${height}-snapshot.txt`,await page.locator('body').ariaSnapshot());
  report.viewports.push(await page.locator('main').getByText(/^SQL CONSOLE/).evaluate(title=>{
   const main=document.querySelector('main');
   const console=title.parentElement;
   const editor=console.children[1];
   const style=el=>{const c=getComputedStyle(el);return {font:c.font,color:c.color,background:c.backgroundColor,padding:c.padding,borderRadius:c.borderRadius,width:el.getBoundingClientRect().width};};
   return {width:innerWidth,height:innerHeight,dpr:devicePixelRatio,main:style(main),nav:style(document.querySelector('nav')),aside:style(document.querySelector('aside')),console:style(console),title:style(title),editor:style(editor),bodyScrollWidth:document.body.scrollWidth};
  }));
 }
 expect(report.page_errors).toEqual([]);
 report.exit_code=0;
}catch(error){report.exit_code=1;report.error=error.stack;await page.screenshot({path:folder+'/first-failure.png'});await writeFile(folder+'/first-failure-snapshot.txt',await page.locator('body').ariaSnapshot());throw error;}
finally{await writeFile(folder+'/reference-capture.json',JSON.stringify(report,null,2)+'\n');await browser.close();server.kill('SIGTERM');await closed;}
console.log(JSON.stringify(report,null,2));
