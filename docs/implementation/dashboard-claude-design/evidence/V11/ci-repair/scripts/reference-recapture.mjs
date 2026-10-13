import {chromium} from '@playwright/test';
import {writeFile} from 'node:fs/promises';
const destination='/Users/nineofour/Agent-Alfred-v11/.scratch/v11/ci-repair/fixed-viewport-capture';
const audit={clock:'2026-10-12T01:15:00Z',method:'Separate headless context per fixed viewport; original archive unchanged',responses:[],errors:[],snapshots:[]};
const browser=await chromium.launch({headless:true});
try {
  for (const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
    const context=await browser.newContext({viewport:{width,height},deviceScaleFactor:1,reducedMotion:'reduce'});
    const page=await context.newPage();
    page.on('response',response=>{if(response.url().includes('unpkg.com'))audit.responses.push({width,height,url:response.url(),status:response.status()});});
    page.on('pageerror',error=>audit.errors.push(String(error)));
    await page.clock.setFixedTime(new Date(audit.clock));
    await page.goto('http://127.0.0.1:18119/Syrup%20Dashboard.dc.html');
    const navigation=page.getByRole('link',{name:/^Loop/});
    audit.navigation_name=(await navigation.innerText()).replace(/\s+/g,' ');
    await navigation.click();
    await page.getByRole('heading',{name:'Loop',exact:true}).waitFor({state:'attached'});
    await page.evaluate(async()=>{
      await document.fonts.ready;
      const finite=document.getAnimations().filter(animation=>Number.isFinite(animation.effect?.getComputedTiming().endTime));
      await Promise.all(finite.map(animation=>animation.finished.catch(()=>{})));
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    });
    await page.screenshot({path:`${destination}/reference-loop-${width}x${height}.png`,animations:'disabled'});
    const geometry=await page.evaluate(()=>({width:innerWidth,height:innerHeight,clientHeight:document.documentElement.clientHeight}));
    if(geometry.width!==width||geometry.height!==height)throw new Error(JSON.stringify(geometry));
    audit.snapshots.push({width,height,actual_viewport:geometry,main_width:await page.getByRole('main').evaluate(element=>element.getBoundingClientRect().width),accessibility:await page.getByRole('main').ariaSnapshot()});
    await context.close();
    console.log(`${width}x${height} reference captured; geometry exact`);
  }
  await writeFile(`${destination}/reference-capture.json`,JSON.stringify(audit,null,2));
}finally{await browser.close();}
