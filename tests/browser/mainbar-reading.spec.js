import {test,expect} from '@playwright/test';
import {controlledTransport,domain} from './transport.js';

async function longHistory(page){
  const session=await controlledTransport(page);
  await page.route('**/api/mainbar?*',route=>route.fulfill({json:{items:[{type:'run_pair',run_id:'old',activity_revision:1,user:[{type:'text',text:'原请求'}],assistant:[{type:'text',text:'完整历史正文。'.repeat(700)}]}],next_cursor:'older-cursor',runs_pending:false}}));
  await page.evaluate(async session=>{const {dashboard}=await import('/assets/app.js');await dashboard.selectSession(session);},session);
  await expect(page.locator('#messages [data-run-id="old"]')).toContainText('完整历史正文');
  await page.locator('#conversation').evaluate(element=>element.scrollTop=0);
  await expect.poll(()=>page.locator('#conversation').evaluate(element=>element.scrollTop)).toBe(0);
  return session;
}
test('new reply preserves a selected old message and only explicit latest follows it',async({page})=>{
  const session=await longHistory(page);
  const selected=await page.locator('#messages [data-run-id="old"] p').last().evaluate(element=>{
    const range=document.createRange();range.setStart(element.firstChild,0);range.setEnd(element.firstChild,12);const selection=getSelection();selection.removeAllRanges();selection.addRange(range);return selection.toString();
  });
  await domain(page,1,session,{name:'run.finished',outcome:'completed',reply_disposition:'reply',reply:{blocks:[{type:'text',text:'刚到的最新回复'}]}});
  await expect(page.locator('#shell-status')).toContainText('有新回复');
  expect(await page.evaluate(()=>getSelection().toString())).toBe(selected);
  expect(await page.locator('#conversation').evaluate(element=>element.scrollTop)).toBe(0);
  await page.getByRole('button',{name:'回到最新',exact:true}).click();
  await expect(page.locator('#messages [data-run-id="r1"]')).toBeInViewport();
  await expect(page.locator('#shell-status')).not.toContainText('有新回复');
});

test('opening hidden MainBar at an old position does not acknowledge the new reply',async({page})=>{
  const session=await longHistory(page);
  await page.getByRole('button',{name:'收起主对话',exact:true}).click();
  await domain(page,1,session,{name:'run.finished',outcome:'completed',reply_disposition:'reply',reply:{blocks:[{type:'text',text:'隐藏期间的新回复'}]}});
  await expect(page.locator('#mainbar')).toBeHidden();await expect(page.locator('#shell-status')).toContainText('有新回复');
  await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  await expect(page.locator('#shell-status')).toContainText('有新回复');
  await page.getByRole('button',{name:'回到最新',exact:true}).click();
  await expect(page.locator('#shell-status')).not.toContainText('有新回复');
  expect(await page.evaluate(session=>JSON.parse(sessionStorage.getItem('alfred.unread:'+session)).seen,session)).toBe(true);
});
