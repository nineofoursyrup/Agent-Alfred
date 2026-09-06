import { test, expect } from "@playwright/test";
import { controlledTransport, emit, state, run } from "./transport.js";

test("known busy and a 409 use one ordinary card and keep the draft", async ({
  page,
}) => {
  const session = await controlledTransport(page);
  const current = run(session);
  await emit(
    page,
    "state_patch",
    state(session, 1, { coordinator_state: "running", active_run: current }),
  );
  const input = page.getByRole("textbox", { name: "消息" });
  await input.fill("等你完成后再发");
  await expect(
    page.getByRole("button", { name: "发送", exact: true }),
  ).toBeDisabled();
  const card = page.getByRole("region", { name: "当前运行" });
  await expect(card).toContainText("终端的问题");
  await expect(card).toContainText("CLI");
  const before = await card.textContent();
  await page.route("**/api/runs", (route) =>
    route.fulfill({
      status: 409,
      json: {
        code: "run_in_progress",
        active_run_summary: {
          purpose: "chat",
          gateway: "cli",
          started_at: current.started_at,
          current_step: null,
          prompt_preview: current.prompt_preview,
          stage: "运行中",
          navigation: {
            href: "/runs/r1?filter=chat",
            run_id: "r1",
            filter: "chat",
          },
        },
      },
    }),
  );
  await emit(page, "state_patch", state(session, 2));
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(card).toHaveText(before);
  await expect(input).toHaveValue("等你完成后再发");
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  await expect(page.getByRole("region", { name: "主对话" })).not.toContainText(
    "等你完成后再发",
  );
});
