from pathlib import Path
root=Path.cwd(); dest=root/'.scratch/v11/probe-tests';dest.mkdir(exist_ok=True)
s=(root/'tests/browser/session-create-race.spec.js').read_text()
s=s.replace("from '@playwright/test'", "from './probe-fixture.js'")
s=s.replace("from './memory-server.js'",f"from '{root}/tests/browser/memory-server.js'")
s="import {writeFile} from 'node:fs/promises';\n"+s
needle="await expect(inspect).toBeFocused();await expect(inspect).toBeInViewport({ratio:1});"
probe="""await expect(inspect).toBeFocused();
    await writeFile(testInfo.outputPath('receipt-layout.json'),JSON.stringify(await inspect.evaluate(element=>{
      const result=[];for(let target=element;target;target=target.parentElement){const b=target.getBoundingClientRect(),s=getComputedStyle(target);result.push({tag:target.tagName,id:target.id,class:target.className,rect:{x:b.x,y:b.y,width:b.width,height:b.height,top:b.top,bottom:b.bottom},scrollTop:target.scrollTop,scrollHeight:target.scrollHeight,clientHeight:target.clientHeight,overflow:s.overflow,overflowY:s.overflowY,display:s.display,position:s.position,lineHeight:s.lineHeight,padding:s.padding,margin:s.margin,border:s.border});}
      return {ancestors:result,root:document.documentElement.clientHeight,viewport:innerHeight,active:document.activeElement.outerHTML,inbox:document.querySelector('.page-body').getBoundingClientRect().toJSON(),bodyScroll:document.scrollingElement.scrollTop};
    }),null,2));
    await expect(inspect).toBeInViewport({ratio:1});"""
assert s.count(needle)==1
s=s.replace(needle,probe)
(dest/'layout-probe.spec.js').write_text(s)
(dest/'probe-fixture.js').write_text("""import {test as base,expect} from '@playwright/test';
export {expect};
export const test=base.extend({page:async({page},use)=>{
  if(process.env.V11_BASELINE_PROBE==='1'){
    await page.route('**/assets/inbox.js',route=>route.fulfill({path:'"""+str(root)+"""/.scratch/v11/baseline-inbox.js',contentType:'text/javascript'}));
    await page.route('**/assets/inbox-runs.css',route=>route.fulfill({path:'"""+str(root)+"""/.scratch/v11/baseline-inbox-runs.css',contentType:'text/css'}));
  }
  await use(page);
}});
""")
(root/'.scratch/v11/probe.config.js').write_text("import original from '../../playwright.config.js';\nexport default {...original,testDir:'"+str(dest)+"',outputDir:'"+str(root/'output/playwright/results')+"',webServer:{...original.webServer,cwd:'"+str(root)+"'}};\n")
