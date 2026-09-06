import {spawn} from "node:child_process";
import {once} from "node:events";
import {createInterface} from "node:readline";
import {createServer} from "node:net";
import {test,expect} from "@playwright/test";

for (const stdinState of ["open", "eof"]) {
test(`CLI startup failure exits and reports the original error with stdin ${stdinState}`, async () => {
  const occupied = createServer();
  let server;
  let closed;
  let stdout = "";
  let stderr = "";
  let exitedWithoutInput = false;
  try {
    occupied.listen(0, "127.0.0.1");
    await once(occupied, "listening");
    const port = occupied.address().port;
    server = spawn(".venv/bin/python", ["-B","-c","import sys; sys.path.insert(0, 'tests/browser'); from recording_server import cli_flow; cli_flow(int(sys.argv[1]))",String(port)], {
      env:{PATH:process.env.PATH,TMPDIR:process.env.TMPDIR,PYTHON_DOTENV_DISABLED:"1",PYTHONDONTWRITEBYTECODE:"1"},
    });
    closed = new Promise(resolve => server.once("close", resolve));
    server.stdout.on("data", data => {stdout += data;});
    server.stderr.on("data", data => {stderr += data;});
    if (stdinState === "eof") server.stdin.end();
    try {
      // Safety deadline, not a delay or race trigger: the port stays owned
      // and the open-stdin case sends no input until the child exits/fails.
      await once(server, "exit", {signal:AbortSignal.timeout(5000)});
      exitedWithoutInput = true;
    } catch (error) {
      if (error.name !== "AbortError") throw error;
    }
  } finally {
    try {
      if (server) {
        if (server.exitCode === null && server.signalCode === null) server.kill("SIGKILL");
        await closed;
        server.stdin.destroy();
      }
    } finally {
      if (occupied.listening) {
        await new Promise((resolve,reject) => occupied.close(error => error ? reject(error) : resolve()));
      }
    }
  }
  expect(stdout).toContain("dashboard unavailable:");
  expect(stdout).toMatch(/address already in use/i);
  expect(stdout).not.toContain("model-entered");
  expect(exitedWithoutInput, "CLI startup failure must exit without waiting for stdin").toBe(true);
  expect(server.signalCode).toBeNull();
  expect(server.exitCode).toBe(1);
  expect(stderr).toContain("RuntimeError: CLI returned 1");
});
}

test("the real CLI entry holds admission while a Web UI send preserves its draft", async ({page}) => {
  const port = Number(process.env.ALFRED_BROWSER_TEST_PORT || 17736) + 4;
  const server = spawn(".venv/bin/python", ["-B","-c","import sys; sys.path.insert(0, 'tests/browser'); from recording_server import cli_flow; cli_flow(int(sys.argv[1]))",String(port)], {
    env:{PATH:process.env.PATH,TMPDIR:process.env.TMPDIR,PYTHON_DOTENV_DISABLED:"1",PYTHONDONTWRITEBYTECODE:"1"},
  });
  let stderr = "";
  server.stderr.on("data", data => {stderr += data;});
  const lines = createInterface({input:server.stdout});
  try {
    await Promise.race([
      new Promise(resolve => lines.on("line", line => {if(line === "model-entered") resolve();})),
      once(server,"exit").then(() => {throw new Error(stderr);}),
    ]);
    const origin = `http://127.0.0.1:${port}`;
    await page.goto(origin);
    await page.getByRole("button",{name:"CLI 闩锁运行",exact:true}).click();
    await page.getByRole("button",{name:"继续此会话",exact:true}).click();
    const input = page.getByRole("textbox",{name:"消息"});
    await input.fill("CLI 忙时的 Web 草稿");
    const busy = page.getByRole("region",{name:"当前运行"});
    await expect(busy).toContainText("CLI");
    await expect(busy).toContainText("运行中");
    const before = await (await page.request.get(`${origin}/api/runs?filter=all`)).json();
    expect(before.non_terminal.gateway).toBe("cli");
    expect(before.runs).toEqual([]);
    const writes = [];
    page.on("request", request => {if(request.method()==="POST" && request.url().endsWith("/api/runs")) writes.push(request.url());});
    await expect(page.getByRole("button",{name:"发送",exact:true})).toBeDisabled();
    await input.press("Enter");
    await expect(input).toHaveValue("CLI 忙时的 Web 草稿");
    const after = await (await page.request.get(`${origin}/api/runs?filter=all`)).json();
    expect(writes).toEqual([]);
    expect(after.runs).toEqual([]);
    expect(after.non_terminal.run_id).toBe(before.non_terminal.run_id);
    expect(after.non_terminal.outcome).toBeNull();
    await expect(page.getByRole("region",{name:"主对话"})).not.toContainText("受控失败");
  } finally {
    server.stdin.end("record\n");
    if(server.exitCode === null) await once(server,"exit");
    lines.close();
    expect(server.exitCode,stderr).toBe(0);
  }
});
