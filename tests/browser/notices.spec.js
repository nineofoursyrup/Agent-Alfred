import { test, expect } from "@playwright/test";
import { controlledTransport, emit, state, run, domain } from "./transport.js";

const causes = {
  malformed: "浏览器检查点无效",
  instance_mismatch: "本地服务已重启",
  too_old: "超出重放窗口",
  ahead: "浏览器提交了服务端从未签发的检查点",
};
const messages = {
  recoverable: "连接已重新同步，继续接收当前运行",
  unrecoverable: "当前运行的过程无法完整恢复；已清除临时文字，等待最终结果",
  absent: "已重新载入保存的对话；当前没有运行",
};
for (const [reason, cause] of Object.entries(causes))
  for (const [recovery, message] of Object.entries(messages)) {
    test(`replay gap ${reason}/${recovery} has connection-only wording and lifetime`, async ({
      page,
    }) => {
      const session = await controlledTransport(page);
      await page.clock.install();
      await emit(page, "transport_notice", {
        code: "replay_gap",
        gap_reason: reason,
        current_run_state: recovery,
      });
      await emit(
        page,
        "state_patch",
        state(
          session,
          1,
          recovery === "absent"
            ? {}
            : { coordinator_state: "running", active_run: run(session) },
        ),
      );
      const notice = page.getByRole("complementary", {
        name: "本标签页连接状态",
      });
      await expect(notice).toContainText(message);
      await expect(notice).toContainText("仅影响本标签页");
      await notice.getByText("连接原因", { exact: true }).click();
      await expect(notice).toContainText(cause);
      await page.clock.runFor(6000);
      if (recovery === "unrecoverable") {
        await expect(notice).toContainText(message);
        await domain(page, 1, session, {
          name: "run.finished",
          outcome: "completed",
          reply: { blocks: [] },
        });
      }
      await expect(notice).toBeHidden();
    });
  }
