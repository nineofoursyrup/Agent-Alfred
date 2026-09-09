import {test, expect} from "@playwright/test";

test("real tool creation and next-Step query reach the chat and survive reload", async ({page}) => {
  await page.goto("/");
  await page.getByRole("button", {name:"新建会话", exact:true}).click();
  await page.getByRole("button", {name:"展开对话", exact:true}).click();
  await page.getByRole("textbox", {name:"消息"}).fill("创建工具测试日程");
  await page.getByRole("button", {name:"发送", exact:true}).click();
  const chat = page.getByRole("region", {name:"主对话"});
  await expect(chat).toContainText("已查询到工具测试日程");
  await expect(chat).toContainText("系统操作回执");
  await page.reload();
  await expect(chat).toContainText("已查询到工具测试日程");
});

for (const action of ["确认", "取消"]) {
  test(`host handles ${action}, reread and recovery without model approval`, async ({page}) => {
    await page.goto("/");
    await page.getByRole("button", {name:"新建会话", exact:true}).click();
    await page.getByRole("button", {name:"展开对话", exact:true}).click();
    const chat = page.getByRole("region", {name:"主对话"});
    const send = async (text) => {
      await page.getByRole("textbox", {name:"消息"}).fill(text);
      await page.getByRole("button", {name:"发送", exact:true}).click();
    };
    await send(`创建${action}测试Skill`);
    await expect(chat).toContainText('"candidate_id"');
    const text = await chat.textContent();
    const id = text.match(/"candidate_id":\s*"([a-f0-9]+)"/)[1];
    await send(`查看候选 ${id}`);
    await expect(chat).toContainText("完整浏览器候选正文");
    await send(`${action === "确认" ? "确认创建" : "取消"} ${id}`);
    await expect(chat).toContainText(action === "确认" ? '"state": "complete"' : '"state": "cancelled"');
    if (action === "确认") {
      await expect(chat).toContainText("重启后加载");
      await send(`查看操作 ${id}`);
      await send(`恢复操作 ${id}`);
      await expect(chat).toContainText('"state": "complete"');
    }
    await page.reload();
    await expect(chat).toContainText(action === "确认" ? '"state": "complete"' : '"state": "cancelled"');
  });
}
