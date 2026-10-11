async (page) => {
 const root='/Users/nineofour/Agent-Alfred-v05/docs/implementation/dashboard-claude-design/evidence/V05';
 const pairs=[];
 await page.route('**/v05-comparison/*.png',route=>route.fulfill({path:`${root}/${new URL(route.request().url()).pathname.split('/').pop()}`,contentType:'image/png'}));
 for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) for(const kind of ['runs','detail']) {
  await page.setViewportSize({width:2*width,height});
  await page.setContent(`<style>body{margin:0;display:flex;width:${2*width}px;height:${height}px}img{display:block;width:${width}px;height:${height}px}</style><img alt="原稿" src="http://127.0.0.1:17928/v05-comparison/reference-loop-${width}x${height}.png"><img alt="Alfred ${kind}" src="http://127.0.0.1:17928/v05-comparison/${kind}-${width}x${height}.png">`);
  await page.locator('img').evaluateAll(nodes=>Promise.all(nodes.map(n=>n.decode())));
  const file=`comparison-${kind}-${width}x${height}.png`;
  await page.screenshot({path:`${root}/${file}`});
  pairs.push({file,viewport:{width,height},composite:{width:2*width,height},left:`reference-loop-${width}x${height}.png`,right:`${kind}-${width}x${height}.png`,scale:1});
 }
 await page.unroute('**/v05-comparison/*.png');
 return pairs;
}
