from pathlib import Path
import subprocess

root = Path.cwd()
scratch = root / '.scratch/v11/ci-repair'
tests = scratch / 'probe-tests'
tests.mkdir(parents=True, exist_ok=True)
for label, ref in [('main', 'origin/main'), ('before', '421ee2a')]:
    for name in ['inbox.js', 'inbox-runs.css']:
        (scratch / f'{label}-{name}').write_bytes(subprocess.check_output([
            'git', 'show', f'{ref}:src/agent_alfred/ops/static/{name}'
        ]))

source = (root / 'tests/browser/session-create-race.spec.js').read_text()
source = source.replace("from '@playwright/test'", "from './fixture.js'")
source = source.replace("from './memory-server.js'", f"from '{root}/tests/browser/memory-server.js'")
measure = '''import {writeFile} from 'node:fs/promises';
async function measure(locator, testInfo, phase) {
  const data = await locator.evaluate(async element => {
    const ancestors = [];
    for (let target = element; target; target = target.parentElement) {
      const b = target.getBoundingClientRect(), s = getComputedStyle(target);
      ancestors.push({tag:target.tagName,id:target.id,class:target.className,
        rect:{x:b.x,y:b.y,width:b.width,height:b.height,top:b.top,bottom:b.bottom},
        scrollTop:target.scrollTop,scrollHeight:target.scrollHeight,clientHeight:target.clientHeight,
        overflow:s.overflow,overflowY:s.overflowY,display:s.display,position:s.position,
        lineHeight:s.lineHeight,padding:s.padding,margin:s.margin,border:s.border});
    }
    const intersectionRatio = await new Promise(resolve => {
      const observer = new IntersectionObserver(entries => { observer.disconnect(); resolve(entries[0].intersectionRatio); });
      observer.observe(element);
    });
    const controls = Array.from(document.querySelectorAll('.inbox-actions,.inbox-read-controls,.sy-list-header')).map(target => {
      const s = getComputedStyle(target);
      return {class:target.className,rect:target.getBoundingClientRect().toJSON(),padding:s.padding};
    });
    return {ancestors,root:document.documentElement.clientHeight,viewport:innerHeight,
      active:document.activeElement.outerHTML,inbox:document.querySelector('.page-body').getBoundingClientRect().toJSON(),
      bodyScroll:document.scrollingElement.scrollTop,intersectionRatio,controls,
      connected:(await import('/assets/app.js')).dashboard.runtime().connected,
      streamStates:window.sessionActionStreams.map(stream => stream.readyState),userAgent:navigator.userAgent};
  });
  await writeFile(testInfo.outputPath(`receipt-layout-${phase}.json`), JSON.stringify(data, null, 2));
}
'''
for name, phase in [('inspect', 'inspect-focused'), ('open', 'open-focused')]:
    needle = f'await expect({name}).toBeFocused();await expect({name}).toBeInViewport({{ratio:1}});'
    assert source.count(needle) == 1
    source = source.replace(needle, f'await expect({name}).toBeFocused();await measure({name},testInfo,"{phase}");await expect({name}).toBeInViewport({{ratio:1}});')
(tests / 'layout.spec.js').write_text(measure + source)
(tests / 'fixture.js').write_text('''import {test as base,expect} from '@playwright/test';
export {expect};
export const test=base.extend({page:async({page},use)=>{
  await page.clock.setFixedTime(new Date('2026-10-12T01:15:00Z'));
  const mode=process.env.V11_RECEIPT_LAYOUT;
  if(mode==='main'||mode==='before') {
    for(const [name,contentType] of [['inbox.js','text/javascript'],['inbox-runs.css','text/css']])
      await page.route(`**/assets/${name}`,route=>route.fulfill({path:`'''+str(scratch)+'''/${mode}-${name}`,contentType}));
  }
  await use(page);
}});
''')
(scratch / 'probe.config.js').write_text("import original from '../../../playwright.config.js';\nexport default {...original,testDir:'" + str(tests) + "',outputDir:'" + str(scratch) + "/measurements/'+process.env.V11_RECEIPT_LAYOUT,webServer:{...original.webServer,cwd:'" + str(root) + "'}};\n")
print('Prepared same measurement script for main, before and after; formal spec unchanged.')
