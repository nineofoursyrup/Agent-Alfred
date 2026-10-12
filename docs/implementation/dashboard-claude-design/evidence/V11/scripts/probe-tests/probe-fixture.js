import {test as base,expect} from '@playwright/test';
export {expect};
export const test=base.extend({page:async({page},use)=>{
  if(process.env.V11_BASELINE_PROBE==='1'){
    await page.route('**/assets/inbox.js',route=>route.fulfill({path:'/Users/nineofour/Agent-Alfred-v11/.scratch/v11/baseline-inbox.js',contentType:'text/javascript'}));
    await page.route('**/assets/inbox-runs.css',route=>route.fulfill({path:'/Users/nineofour/Agent-Alfred-v11/.scratch/v11/baseline-inbox-runs.css',contentType:'text/css'}));
  }
  if(process.env.V11_PROPOSED_LAYOUT_PROBE==='1')await page.route('**/assets/inbox-runs.css',route=>route.fulfill({path:'/Users/nineofour/Agent-Alfred-v11/.scratch/v11/proposed-inbox-runs.css',contentType:'text/css'}));
  await use(page);
}});
