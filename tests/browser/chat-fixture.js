import {test as base, expect} from '@playwright/test';
import {memoryServer} from './memory-server.js';

export const test = base.extend({
  chatServer: async ({}, use) => {
    const server = await memoryServer({script:'tests/browser/chat_server.py'});
    try { await use(server); } finally { await server.close(); }
  },
});

export async function createChatSession(page) {
  const created = page.waitForResponse(r => r.url().endsWith('/api/sessions') && r.request().method() === 'POST');
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  const response = await created, body = await response.json();
  expect(response.status(), JSON.stringify(body)).toBe(201);
  expect(body.session_id).toEqual(expect.any(String));
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('alfred.session'))).toBe(body.session_id);
  return body.session_id;
}

export async function sendChat(page, server, text) {
  const session = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
  expect(session).toEqual(expect.any(String));
  await page.getByRole('textbox',{name:'消息',exact:true}).fill(text);
  const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
  await page.getByRole('button',{name:'发送',exact:true}).click();
  const response = await accepted, body = await response.json();
  expect(response.status(), JSON.stringify(body)).toBe(202);
  expect(response.request().postDataJSON()).toMatchObject({message:text, session_id:session});
  expect(body).toMatchObject({run_id:expect.any(String), session_id:session});
  // A visible reply/idle snapshot precedes the saved-chat scheduling callback.
  await server.send('wait-settled ' + body.run_id);
  return body;
}
