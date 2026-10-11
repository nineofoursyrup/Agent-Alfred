async (page) => {
 const root="/Users/nineofour/Agent-Alfred-v05/docs/implementation/dashboard-claude-design/evidence/V05";
 const pairs=[],processPairs=[];
 for(const [width,height] of [[1440,900],[1280,800],[390,844],[320,800]]) for(const kind of ["runs","detail",...(width<=390?["process"]:[])]) {
  const context=await page.context().browser().newContext({viewport:{width:width*2,height}}),current=await context.newPage();
  await current.route("**/v05-comparison/*.png",route=>route.fulfill({path:`${root}/${new URL(route.request().url()).pathname.split("/").pop()}`,contentType:"image/png"}));
  const left=`reference-loop-${width}x${height}.png`,right=`${kind}-${width}x${height}.png`;
  await current.setContent(`<style>body{margin:0;display:flex;width:${2*width}px;height:${height}px}img{display:block;width:${width}px;height:${height}px}</style><img alt="原稿" src="http://127.0.0.1:17928/v05-comparison/${left}"><img alt="Alfred ${kind}" src="http://127.0.0.1:17928/v05-comparison/${right}">`);
  await current.locator("img").evaluateAll(nodes=>Promise.all(nodes.map(node=>node.decode())));
  const file=`comparison-${kind}-${width}x${height}.png`;await current.screenshot({path:`${root}/${file}`});
  const entry={file,viewport:{width,height},composite:{width:2*width,height},left,right,scale:1,reference:"reuse unchanged authoritative reference PNG; repair final production recaptured"};
  (kind==="process"?processPairs:pairs).push(entry);await context.close();
 }
 return {pairs,processPairs};
}
