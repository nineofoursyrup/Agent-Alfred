import {expect} from "@playwright/test";
import {test, createChatSession, sendChat} from "./chat-fixture.js";

test("real tool creation and next-Step query reach the chat and survive reload", async ({page, chatServer}) => {
  await page.goto(chatServer.origin + "/");
  await createChatSession(page);
  await page.getByRole("button", {name:"展开对话", exact:true}).click();
  await sendChat(page, chatServer, "创建工具测试日程");
  const chat = page.getByRole("region", {name:"主对话"});
  await expect(chat).toContainText("已查询到工具测试日程");
  await expect(chat).toContainText("系统操作回执");
  await page.reload();
  await expect(chat).toContainText("已查询到工具测试日程");
});

for (const action of ["确认", "取消"]) {
  test(`host handles ${action}, reread and recovery without model approval`, async ({page, chatServer}) => {
    await page.goto(chatServer.origin + "/");
    await createChatSession(page);
    await page.getByRole("button", {name:"展开对话", exact:true}).click();
    const chat = page.getByRole("region", {name:"主对话"});
    const send = text => sendChat(page, chatServer, text);
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

for (const next of ['session', 'run']) {
  test(`CI-02: ${next} admission during saved-chat scheduling preserves the draft and recovers`, async ({page, context, chatServer}) => {
    await page.goto(chatServer.origin + '/');
    const session = await createChatSession(page);
    await page.getByRole('button',{name:'展开对话',exact:true}).click();
    await chatServer.send('hold-scheduling');
    try {
      await page.getByRole('textbox',{name:'消息',exact:true}).fill('创建确认测试Skill');
      const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
      await page.getByRole('button',{name:'发送',exact:true}).click();
      const initial = await accepted, first = await initial.json();
      expect(initial.status()).toBe(202);
      expect(first).toMatchObject({run_id:expect.any(String), session_id:session});
      await chatServer.send('wait-scheduling');
      const chat = page.getByRole('region',{name:'主对话'});
      await expect(chat).toContainText('"candidate_id"');
      const id = (await chat.textContent()).match(/"candidate_id":\s*"([a-f0-9]+)"/)[1];
      // A fresh context models the next test; another Run uses the same Session.
      const isolated = next === 'session' ? await context.browser().newContext() : null;
      const target = isolated ? await isolated.newPage() : page;
      try {
        if (isolated) await target.goto(chatServer.origin + '/inbox');
        else await target.getByRole('textbox',{name:'消息',exact:true}).fill(`查看候选 ${id}`);
        const path = next === 'session' ? '/api/sessions' : '/api/runs';
        const refused = target.waitForResponse(r => r.url().endsWith(path) && r.request().method() === 'POST');
        await target.getByRole('button',{name:next === 'session' ? '新建会话' : '发送',exact:true}).click();
        const response = await refused;
        expect(response.status()).toBe(409);
        expect(await response.json()).toMatchObject({code:'mutation_in_flight'});
        if (isolated) await expect(target.getByRole('textbox',{name:'消息',exact:true})).toBeDisabled();
        else await expect(target.getByRole('textbox',{name:'消息',exact:true})).toHaveValue(`查看候选 ${id}`);
        await chatServer.send('release-scheduling');
        await chatServer.send('wait-settled ' + first.run_id);
        if (isolated) {
          const newSession = await createChatSession(target);
          expect(newSession).not.toBe(session);
          await target.getByRole('button',{name:'展开对话',exact:true}).click();
          await sendChat(target, chatServer, '创建工具测试日程');
          await expect(target.getByRole('region',{name:'主对话'})).toContainText('已查询到工具测试日程');
        } else {
          await sendChat(page, chatServer, `查看候选 ${id}`);
          await sendChat(page, chatServer, `确认创建 ${id}`);
          await expect(chat).toContainText('"state": "complete"');
        }
      } finally { await isolated?.close(); }
    } finally { await chatServer.send('release-scheduling'); }
  });
}
