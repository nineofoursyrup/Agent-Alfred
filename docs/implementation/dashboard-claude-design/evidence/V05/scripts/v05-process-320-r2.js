async (page) => {
 const root='docs/implementation/dashboard-claude-design/evidence/V05',target='0fea87cbb46c45b1afce86d9a1b1a9bc';
 await page.setViewportSize({width:320,height:800});
 await page.goto(`http://127.0.0.1:17920/runs/${target}`);
 await page.getByRole('region',{name:'运行过程',exact:true}).getByText('过程记录可用 · available',{exact:true}).waitFor();
 const result=[];
 for(const [width,height] of [[320,800]]){
  await page.setViewportSize({width,height});
  await page.locator('.run-process').evaluate(n=>n.scrollIntoView({block:'start'}));
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await page.screenshot({path:`${root}/process-${width}x${height}.png`});
  result.push({width,height,pageScroll:await page.locator('#page').evaluate(n=>n.scrollTop),overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),process:await page.locator('.run-process').ariaSnapshot()});
 }
 return result;
}
