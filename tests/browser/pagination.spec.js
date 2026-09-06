import {test, expect} from "@playwright/test";

test("Session, chat Run and all Run pages keep independent opaque cursors", async ({page}) => {
  const visited = {sessions:[], chat:[], runs:[]};
  const paged = (kind, cursor, make) => {
    visited[kind].push(cursor);
    return {items: Array.from({length:cursor ? 2 : 25}, (_,i) => make(i+(cursor ? 25 : 0))), next_cursor:cursor ? null : `${kind} opaque /?`};
  };
  await page.route("**/api/sessions?*", route => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    const result = paged("sessions", cursor, i => ({session_id:`s${i}`, title:`分页会话 ${i}`, created_at:"today"}));
    return route.fulfill({json:{sessions:result.items, next_cursor:result.next_cursor}});
  });
  await page.route("**/api/sessions/messages?*", route => route.fulfill({json:{messages:[],next_cursor:null}}));
  await page.route("**/api/sessions/runs?*", route => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    const result = paged("chat", cursor, i => ({run_id:`chat-${i}`, gateway:"cli", accepted_at:`time-${i}`, outcome:"completed", reply_preview:`会话回复 ${i}`}));
    return route.fulfill({json:{runs:result.items, next_cursor:result.next_cursor}});
  });
  await page.route("**/api/runs?*", route => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    const result = paged("runs", cursor, i => ({run_id:`run-${i}`, purpose:"chat", filter:"chat", accepted_at:`time-${i}`, phase:"finished", outcome:"completed"}));
    return route.fulfill({json:{filter:"all", runs:result.items, next_cursor:result.next_cursor}});
  });
  await page.goto("/");
  const main = page.getByRole("main");
  await expect(main.getByRole("button", {name:/^分页会话 /})).toHaveCount(25);
  await main.getByRole("button", {name:"更多会话",exact:true}).click();
  await expect(main.getByRole("button", {name:/^分页会话 /})).toHaveCount(27);
  await main.getByRole("button", {name:"分页会话 26",exact:true}).click();
  const preview = page.getByRole("region", {name:"会话只读预览"});
  await expect(preview.getByRole("link", {name:"查看运行"})).toHaveCount(25);
  await preview.getByRole("button", {name:"更多会话运行",exact:true}).click();
  await expect(preview.getByRole("link", {name:"查看运行"})).toHaveCount(27);
  await page.getByRole("link", {name:"运行",exact:true}).click();
  const runs = page.getByRole("region", {name:"运行列表"});
  await expect(runs.getByRole("link", {name:"查看运行"})).toHaveCount(25);
  await main.getByRole("button", {name:"更多运行",exact:true}).click();
  await expect(runs.getByRole("link", {name:"查看运行"})).toHaveCount(27);
  expect(visited).toEqual({sessions:[null,"sessions opaque /?"],chat:[null,"chat opaque /?"],runs:[null,"runs opaque /?"]});
});

for (const [outcome, label] of [["completed","回复完成"],["max_steps","受控停止"],["failed","受控失败"],["interrupted","运行终态无法确认"]]) {
  test(`${outcome} has distinct visible and accessible Run wording`, async ({page}) => {
    await page.route("**/api/runs?*", route => route.fulfill({json:{filter:"all", next_cursor:null, runs:[{run_id:"outcome", purpose:"chat", phase:"finished", outcome, started_at:"today"}]}}));
    await page.goto("/runs");
    const list = page.getByRole("region", {name:"运行列表"});
    await expect(list.getByText(label,{exact:true})).toBeVisible();
    expect(await list.ariaSnapshot()).toContain(label);
    await expect(list.getByRole("link", {name:"查看运行"})).toBeVisible();
  });
}
