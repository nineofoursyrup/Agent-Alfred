import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { once } from "node:events";
import { test, expect } from "@playwright/test";

for (const fails of [false, true])
  test(`real pending reconnect and second-tab draft survive ${fails ? "failed" : "recorded"} settlement`, async ({
    page,
    context,
  }) => {
    const port = Number(process.env.ALFRED_BROWSER_TEST_PORT || 17736) + 1;
    const server = spawn(
      ".venv/bin/python",
      ["tests/browser/recording_server.py", "--port", String(port)],
      { stdio: ["pipe", "pipe", "pipe"] },
    );
    const seen = new Set();
    const waiters = new Map();
    const lines = createInterface({ input: server.stdout });
    lines.on("line", (line) => {
      seen.add(line);
      waiters.get(line)?.();
    });
    const waitFor = (line) =>
      seen.has(line)
        ? Promise.resolve()
        : new Promise((resolve) => waiters.set(line, resolve));
    let stderr = "";
    server.stderr.on("data", (data) => {
      stderr += data;
    });
    try {
      await Promise.race([
        waitFor("ready"),
        once(server, "exit").then(() => {
          throw new Error(stderr);
        }),
      ]);
      const origin = `http://127.0.0.1:${port}`;
      await page.goto(origin);
      await page.getByRole("button", { name: "新建会话", exact: true }).click();
      const other = await context.newPage();
      await other.goto(origin);
      await other
        .getByRole("main")
        .getByRole("button", { name: /新会话 ·/ })
        .click();
      await other
        .getByRole("button", { name: "继续此会话", exact: true })
        .click();
      await other.getByRole("textbox", { name: "消息" }).fill("第二标签页草稿");
      await page.getByRole("button", { name: "展开对话", exact: true }).click();
      await page.getByRole("textbox", { name: "消息" }).fill("保存窗口测试");
      await page.getByRole("button", { name: "发送", exact: true }).click();
      await waitFor("pending");
      await expect(page.getByRole("region", { name: "主对话" })).toContainText(
        "离线模型回复",
      );
      await expect(
        other.getByRole("region", { name: "当前运行" }),
      ).toContainText("正在保存");
      const conflict = await other.evaluate(async () => {
        const entry = await (await fetch("/api/entry")).json();
        return (
          await fetch("/api/runs", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "x-agent-alfred-csrf": entry.csrf_token,
            },
            body: JSON.stringify({
              session_id: sessionStorage.getItem("alfred.session"),
              message: "竞态提交",
            }),
          })
        ).status;
      });
      expect(conflict).toBe(409);
      await other.reload();
      const chat = other.getByRole("region", { name: "主对话" });
      await expect(chat.getByText("离线模型回复", { exact: true })).toHaveCount(
        1,
      );
      await expect(chat).toContainText("正在保存");
      await expect(other.getByRole("textbox", { name: "消息" })).toHaveValue(
        "第二标签页草稿",
      );
      server.stdin.write(fails ? "fail\n" : "record\n");
      await expect(chat).toContainText(fails ? "回复已收到但未保存" : "已保存");
      await expect(chat.getByText("离线模型回复", { exact: true })).toHaveCount(
        1,
      );
      if (fails) {
        const response = await other.evaluate(async () => {
          const entry = await (await fetch("/api/entry")).json();
          return (
            await fetch("/api/runs", {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "x-agent-alfred-csrf": entry.csrf_token,
              },
              body: JSON.stringify({
                session_id: sessionStorage.getItem("alfred.session"),
                message: "记录失败后",
              }),
            })
          ).status;
        });
        expect(response).toBe(503);
      }
      await other.close();
    } finally {
      server.stdin.end("stop\n");
      if (server.exitCode === null) await once(server, "exit");
      lines.close();
      expect(server.exitCode, stderr).toBe(0);
    }
  });
