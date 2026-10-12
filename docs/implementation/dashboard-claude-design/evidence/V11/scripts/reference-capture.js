async (page) => {
  const base = '/Users/nineofour/Agent-Alfred-v11/.scratch/v11/capture';
  const audit = {responses: [], errors: [], snapshots: []};
  page.on('response', r => {if (r.url().includes('unpkg.com')) audit.responses.push({url: r.url(), status: r.status()});});
  page.on('pageerror', e => audit.errors.push(String(e)));
  await page.reload();
  const navigation = page.getByRole('link', {name: /^Loop/});
  audit.navigation_name = await navigation.innerText();
  await navigation.click();
  await page.getByRole('heading', {name: 'Loop', exact: true}).waitFor();
  for (const [width, height] of [[1440,900],[1280,800],[390,844],[320,800]]) {
    await page.setViewportSize({width, height});
    await page.screenshot({path: `${base}/reference-loop-${width}x${height}.png`, animations: 'disabled'});
    audit.snapshots.push({width, height, main_width: await page.getByRole('main').evaluate(el => el.getBoundingClientRect().width),
      accessibility: await page.getByRole('main').ariaSnapshot()});
  }
  return audit;
}
