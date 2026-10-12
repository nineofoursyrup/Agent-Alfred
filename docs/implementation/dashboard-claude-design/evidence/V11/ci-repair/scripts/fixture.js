import {test as base,expect} from '@playwright/test';
export {expect};
export const test=base.extend({page:async({page},use)=>{
  await page.clock.setFixedTime(new Date('2026-10-12T01:15:00Z'));
  const mode=process.env.V11_RECEIPT_LAYOUT;
  if(mode==='main'||mode==='before') {
    for(const [name,contentType] of [['inbox.js','text/javascript'],['inbox-runs.css','text/css']])
      await page.route(`**/assets/${name}`,route=>route.fulfill({path:`/Users/nineofour/Agent-Alfred-v11/.scratch/v11/ci-repair/${mode}-${name}`,contentType}));
  }
  await use(page);
}});
