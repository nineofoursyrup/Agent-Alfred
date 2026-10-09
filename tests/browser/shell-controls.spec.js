import {test,expect} from '@playwright/test';

for(const name of ['models','behaviour','tools'])test(`${name} registers real unsaved input across panel visibility and confirmed navigation`,async({page})=>{
  await page.goto('/'+name);
  if(name==='models')await page.locator("[data-model='opencode-go:deepseek-v4-flash']").getByText('模型详情',{exact:true}).click();
  const control=name==='models'?page.getByRole('textbox',{name:'显示名',exact:true}).first():name==='behaviour'?page.getByRole('checkbox',{name:'启用消息分流'}):page.getByRole('combobox',{name:'web_search 授权草稿'});
  if(name==='models')await control.fill('未保存的模型名称');
  else if(name==='behaviour')await control.check();
  else await control.selectOption('denied');
  await page.getByRole('button',{name:'收起主对话',exact:true}).click();
  await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  await page.locator('nav a[href="/runs"]').click();
  await expect(page.getByRole('button',{name:'留在此页',exact:true})).toBeFocused();
  await page.keyboard.press('Escape');await expect(page).toHaveURL(new RegExp('/'+name+'$'));
  if(name==='models')await expect(control).toHaveValue('未保存的模型名称');
  else if(name==='behaviour')await expect(control).toBeChecked();
  else await expect(control).toHaveValue('denied');
  await page.locator('nav a[href="/runs"]').click();await page.getByRole('button',{name:'放弃并离开',exact:true}).click();
  await expect(page).toHaveURL(/\/runs$/);
});

test('amplified text at reduced available height retains scrollable MainBar controls',async({page})=>{
  // Supplemental font amplification, not the separately required native zoom.
  await page.setViewportSize({width:320,height:360});await page.goto('/inbox');
  await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  await page.locator('#message').fill('仍可编辑');
  await page.evaluate(()=>document.documentElement.style.fontSize='28px');
  await page.mouse.move(100,180);await page.mouse.wheel(0,1200);
  await expect(page.getByRole('button',{name:'发送',exact:true})).toBeInViewport();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});

test('display-name late save advances the dirty baseline while retaining the newer draft',async({page})=>{
  const models=name=>({revision:name==='A'?1:2,status:'ok',endpoints:[{endpoint_id:'fixture',catalog:{health:'fresh'},models:[{endpoint_id:'fixture',model_id:'model',display_name:name,display_name_override:name,pinned:true,assignable:true,price_override:{},support_label:'fixture'}]}]});
  await page.route('**/api/models',route=>route.fulfill({json:models('A')}));
  await page.goto('/models');
  await page.getByText('模型详情',{exact:true}).click();
  const name=page.getByRole('textbox',{name:'显示名',exact:true});await expect(name).toHaveValue('A');
  let release,entered;const gate=new Promise(r=>release=r),held=new Promise(r=>entered=r);
  await page.route('**/api/settings',async route=>{expect(route.request().postDataJSON().display_name).toBe('B');entered();await gate;await route.fulfill({json:models('B')});});
  await name.fill('B');await page.getByRole('button',{name:'保存显示名',exact:true}).click();await held;
  await name.fill('C');release();await expect(page.getByRole('heading',{name:'B',exact:true})).toBeVisible();
  await expect(name).toHaveValue('C');await name.fill('A');
  await page.locator('nav a[href="/runs"]').click();
  await expect(page.getByRole('button',{name:'留在此页',exact:true})).toBeFocused();
  await page.keyboard.press('Escape');await expect(name).toHaveValue('A');
  await name.fill('B');await page.locator('nav a[href="/runs"]').click();
  await expect(page).toHaveURL(/\/runs$/);
});
