from pathlib import Path
root=Path.cwd()
css=(root/'src/agent_alfred/ops/static/inbox-runs.css').read_text()
old='.inbox-page .inbox-read-controls [role="status"] {\n  flex-basis: 100%;'
new='.inbox-page .inbox-read-controls [role="status"] {\n  flex: 1 1 calc(var(--gap-large) * 12);'
assert css.count(old)==1
(root/'.scratch/v11/proposed-inbox-runs.css').write_text(css.replace(old,new))
p=root/'.scratch/v11/probe-tests/probe-fixture.js'
s=p.read_text()
s=s.replace('  await use(page);',"  if(process.env.V11_PROPOSED_LAYOUT_PROBE==='1')await page.route('**/assets/inbox-runs.css',route=>route.fulfill({path:'"+str(root)+"/.scratch/v11/proposed-inbox-runs.css',contentType:'text/css'}));\n  await use(page);")
p.write_text(s)
