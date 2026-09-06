import {spawn} from "node:child_process";
import {once} from "node:events";
import {createInterface} from "node:readline";
import {mkdtemp, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {test, expect} from "@playwright/test";

test("an actual Dashboard process restart preserves an empty Session and its draft", async ({page}) => {
  const directory = await mkdtemp(join(tmpdir(), "alfred-restart-browser-"));
  const port = Number(process.env.ALFRED_BROWSER_TEST_PORT || 17736) + 2;
  let server;
  async function start() {
    server = spawn(".venv/bin/python", ["tests/browser/server.py", "--port", String(port), "--state", directory]);
    let error = "";
    server.stderr.on("data", data => {error += data;});
    const lines = createInterface({input: server.stdout});
    await Promise.race([
      new Promise(resolve => lines.on("line", line => {if (line === "ready") resolve();})),
      once(server, "exit").then(() => {throw new Error(error);}),
    ]);
  }
  async function stop() {
    if (server && server.exitCode === null) {
      const done = once(server, "exit");
      server.kill("SIGTERM");
      await done;
      expect(server.exitCode).toBe(0);
    }
  }
  try {
    await start();
    await page.goto(`http://127.0.0.1:${port}`);
    await page.getByRole("button", {name:"新建会话", exact:true}).click();
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem("alfred.session"))).not.toBeNull();
    const session = await page.evaluate(() => sessionStorage.getItem("alfred.session"));
    await page.getByRole("textbox", {name:"消息"}).fill("真实重启的草稿");
    const refreshedEntry = page.waitForResponse(response => response.url().endsWith("/api/entry"));
    await stop();
    await start();
    await refreshedEntry;
    const accepted = page.waitForResponse(response => response.url().endsWith("/api/runs") && response.request().method() === "POST");
    await expect(page.getByRole("textbox", {name:"消息"})).toHaveValue("真实重启的草稿");
    expect(await page.evaluate(() => sessionStorage.getItem("alfred.session"))).toBe(session);
    await expect(page.getByRole("button", {name:"发送", exact:true})).toBeEnabled({timeout:10000});
    await page.getByRole("button", {name:"展开对话", exact:true}).click();
    await page.getByRole("button", {name:"发送", exact:true}).click();
    expect((await accepted).status()).toBe(202);
    await expect(page.getByRole("region", {name:"主对话"})).toContainText("已保存");
    expect(await page.evaluate(() => sessionStorage.getItem("alfred.session"))).toBe(session);

  } finally {
    await stop();
    await rm(directory, {recursive:true, force:true});
  }
});
