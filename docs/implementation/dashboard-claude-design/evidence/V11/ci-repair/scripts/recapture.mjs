import {chromium} from '@playwright/test';
import {readFile,writeFile,mkdir} from 'node:fs/promises';

const root='/Users/nineofour/Agent-Alfred-v11';
const scratch=`${root}/.scratch/v11`;
const destination=`${scratch}/ci-repair/fixed-viewport-capture`;
await mkdir(destination,{recursive:true});
const browser=await chromium.launch({headless:true});
const combined={clock:'2026-10-12T01:15:00Z',server:'tests/browser/server.py',
  method:'Separate headless context per fixed viewport; no viewport resizing',
  responses:[],errors:[],request_failures:[],snapshots:[],runs_proof:[],geometry:[]};
const proof={clock:combined.clock,server:combined.server,reduced_motion:'reduce',results:[]};
proof.method='Replace only the linked inbox-runs.css in the same /runs document; await stylesheet load. MainBar DOM and focus remain stable.';
try {
  for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
    const context=await browser.newContext({viewport:{width,height},deviceScaleFactor:1,reducedMotion:'reduce'});
    const page=await context.newPage();
    async function settle() {
      await page.evaluate(async()=>{
        await document.fonts.ready;
        const finite=document.getAnimations().filter(animation=>Number.isFinite(animation.effect?.getComputedTiming().endTime));
        await Promise.all(finite.map(animation=>animation.finished.catch(()=>{})));
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      });
    }
    let source=await readFile(`${scratch}/capture.js`,'utf8');
    source=source.replace(`const base = '${scratch}/capture';`,`const base = '${destination}';`)
      .replace('const views = [[1440,900],[1280,800],[390,844],[320,800]];',`const views = [[${width},${height}]];`)
      .replace('await page.setViewportSize({width:1440,height:900});','')
      .replace('await page.setViewportSize({width,height});','');
    source=source.replace('const selected = await page.evaluate(() => sessionStorage.getItem(\'alfred.session\'));',
      `if (${width}<1100 && await page.getByRole('button',{name:'收起主对话',exact:true}).isVisible()) await page.getByRole('button',{name:'收起主对话',exact:true}).click();\n  const selected = await page.evaluate(() => sessionStorage.getItem('alfred.session'));`);
    source=source.replaceAll('await page.screenshot(',`await settle();\n      await page.screenshot(`);
    const capture=await eval(`(${source})`)(page);
    for(const name of ['responses','errors','request_failures','snapshots']) combined[name].push(...capture[name]);
    const geometry=await page.evaluate(()=>({width:innerWidth,height:innerHeight,clientWidth:document.documentElement.clientWidth,clientHeight:document.documentElement.clientHeight,mainCount:document.querySelectorAll('#page').length}));
    if(geometry.width!==width||geometry.height!==height||geometry.mainCount!==1) throw new Error(JSON.stringify(geometry));
    combined.geometry.push({requested:{width,height},actual:geometry});
    let script=await readFile(`${scratch}/runs-proof.js`,'utf8');
    script=script.replace(`const base = '${scratch}/capture';`,`const base = '${destination}';`)
      .replace('await page.setViewportSize({width:1440,height:900});','')
      .replace('[[1440,900],[1280,800],[390,844],[320,800]]',`[[${width},${height}]]`)
      .replace('await page.setViewportSize({width,height});','');
    script=script.replace("await page.route('**/assets/inbox-runs.css',", "await page.route('**/assets/inbox-runs.css?ci-proof=before',")
      .replace('await read();\n    const before =', `await page.evaluate(()=>new Promise((resolve,reject)=>{\n      const link=document.querySelector('link[href="/assets/inbox-runs.css"]');\n      link.addEventListener('load',resolve,{once:true});\n      link.addEventListener('error',reject,{once:true});\n      link.href='/assets/inbox-runs.css?ci-proof=before';\n    }));\n    const before =`)
      .replace("await page.unroute('**/assets/inbox-runs.css');", "await page.unroute('**/assets/inbox-runs.css?ci-proof=before');");
    script=script.replaceAll('await page.screenshot(',`await settle();\n    await page.screenshot(`);
    const result=await eval(`(${script})`)(page);
    proof.results.push(...result.results);
    await context.close();
    console.log(`${width}x${height} captured; geometry exact`);
  }
  await writeFile(`${destination}/capture.json`,JSON.stringify(combined,null,2));
  await writeFile(`${destination}/runs-proof.json`,JSON.stringify(proof,null,2));
} finally {await browser.close();}
