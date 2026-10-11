import {chromium,expect} from '@playwright/test';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {writeFile,readFile,mkdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
expect(process.env.ALFRED_BROWSER_TEST_PORT).toBe('17910');
const evidence='docs/implementation/dashboard-claude-design/evidence/V04';
const directory='docs/design/issue-122/reference';
const source=directory+'/Syrup Dashboard.dc.html';
const server=spawn('.venv/bin/python',['-u','-m','http.server','17918','--bind','127.0.0.1','--directory',directory]);
const closed=once(server,'close');
let serverErrors='';server.stderr.on('data',value=>serverErrors+=value.toString());
await Promise.race([once(server.stdout,'data'),closed.then(()=>{throw new Error(serverErrors);})]);
const browser=await chromium.launch();const page=await browser.newPage();
const report={source_html_sha256:createHash('sha256').update(await readFile(source)).digest('hex'),support_js_sha256:createHash('sha256').update(await readFile(directory+'/support.js')).digest('hex'),source_version:'1786950414092924',view:'Overview',origin:'http://127.0.0.1:17918',browser:browser.version(),viewports:[],console:[],pageErrors:[],network_dependencies:[],captured_at:new Date().toISOString(),animations:'Disabled for screenshot only; original timers remain'};
page.on('console',msg=>{if(msg.type()==='error')report.console.push(msg.text());});
page.on('pageerror',error=>report.pageErrors.push(error.message));
page.on('response',response=>{if(response.url().includes('unpkg.com'))report.network_dependencies.push({url:response.url(),status:response.status()});});
try {
 await page.goto(report.origin+'/Syrup%20Dashboard.dc.html');
 await page.getByRole('link',{name:'Overview',exact:true}).click();
 await expect(page.getByText('ARCHITECTURE',{exact:true})).toBeVisible();
 for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]){
  await page.setViewportSize({width,height});
  await page.evaluate(()=>{for(const node of document.querySelectorAll('*'))if(node.scrollTop)node.scrollTop=0;});
  await page.screenshot({path:`${evidence}/reference-${width}x${height}.png`,animations:'disabled'});
  await page.getByText('ARCHITECTURE',{exact:true}).evaluate(node=>node.scrollIntoView({block:'start'}));
  await page.screenshot({path:`${evidence}/reference-architecture-${width}x${height}.png`,animations:'disabled'});
  await page.getByText('RECENT TURNS',{exact:true}).evaluate(node=>node.scrollIntoView({block:'start'}));
  await page.screenshot({path:`${evidence}/reference-recent-${width}x${height}.png`,animations:'disabled'});
  report.viewports.push({width,height,geometry:await page.evaluate(()=>({viewport:innerWidth,bodyScroll:document.body.scrollWidth}))});
 }
 expect(report.pageErrors).toEqual([]);report.exit_code=0;
} catch(error){
 report.exit_code=1;report.error=error.stack;
 const failed=`${evidence}/first-review-r2-reference-failure`;await mkdir(failed);await page.screenshot({path:failed+'/failed.png'});await writeFile(failed+'/error-context.txt',await page.locator('body').ariaSnapshot());await writeFile(failed+'/reference-audit.json',JSON.stringify(report,null,2)+'\n');throw error;
} finally {
 await writeFile(`${evidence}/reference-audit.json`,JSON.stringify(report,null,2)+'\n');await browser.close();server.kill('SIGTERM');await closed;
}
