import {expect} from '@playwright/test';

// #75 CE-01/07: inspect during real business requests; never submit on their behalf.
export async function observeTopology(page) {
  const controls=page.locator('#page input, #page textarea, #page select');
  const values=()=>controls.evaluateAll(els=>els.map(el=>({value:el.value,checked:el.checked})));
  const before=await values();
  const posts=[]; const listener=r=>{if(r.method()==='POST') posts.push(r.url());};
  page.on('request',listener);
  try {
    for (const name of ['消息分流','手动聚合']) {
      const region=page.getByRole('region',{name,exact:true});
      await region.getByRole('button',{name:'查看流程',exact:true}).click();
      await expect(region.locator('svg')).toBeVisible();
      await region.getByRole('button',{name:'重新读取',exact:true}).click();
      await expect(region.getByText(/重新读取中/)).toHaveCount(0);
      await region.getByRole('button',{name:'收起流程',exact:true}).click();
    }
    expect(await values()).toEqual(before);
    expect(posts).toEqual([]);
  } finally {page.off('request',listener);}
}
