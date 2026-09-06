import { test, expect } from "@playwright/test";
import { controlledTransport, domain, emit, state, run } from "./transport.js";

test("disconnect clears an unfinished Attempt and suppresses its later deltas", async ({
  page,
}) => {
  const session = await controlledTransport(page);
  await emit(
    page,
    "state_patch",
    state(session, 1, {
      coordinator_state: "running",
      active_run: run(session),
    }),
  );
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  const chat = page.getByRole("region", { name: "主对话" });
  await domain(
    page,
    1,
    session,
    { name: "attempt.started", attempt_id: "a1" },
    { attempt: "a1", step: 0 },
  );
  await domain(
    page,
    2,
    session,
    { name: "block.started", index: 0, block_type: "text" },
    { attempt: "a1", step: 0 },
  );
  await domain(
    page,
    3,
    session,
    { name: "block.delta", index: 0, text: "暂时的文字" },
    { attempt: "a1", step: 0 },
  );
  await expect(chat).toContainText("暂时的文字");
  await emit(page, "error", {});
  await expect(chat).not.toContainText("暂时的文字");
  await domain(
    page,
    4,
    session,
    { name: "block.delta", index: 0, text: "旧增量不得回来" },
    { attempt: "a1", step: 0 },
  );
  await domain(
    page,
    5,
    session,
    { name: "attempt.aborted", attempt_id: "a1", blocks: [] },
    { attempt: "a1", step: 0 },
  );
  await domain(
    page,
    6,
    session,
    { name: "attempt.started", attempt_id: "a2" },
    { attempt: "a2", step: 0 },
  );
  await domain(
    page,
    7,
    session,
    { name: "block.started", index: 0, block_type: "text" },
    { attempt: "a2", step: 0 },
  );
  await domain(
    page,
    8,
    session,
    { name: "block.delta", index: 0, text: "新的文字" },
    { attempt: "a2", step: 0 },
  );
  await expect(chat).toContainText("新的文字");
  await expect(chat).not.toContainText("旧增量不得回来");
  await domain(
    page,
    9,
    session,
    {
      name: "attempt.committed",
      attempt_id: "a2",
      blocks: [{ type: "text", text: "工具调用前的文字" }],
      stop_reason: "tool_use",
    },
    { attempt: "a2", step: 0 },
  );
  await expect(chat).not.toContainText("新的文字");
  await expect(chat).not.toContainText("工具调用前的文字");
  await domain(page, 10, session, {
    name: "run.finished",
    outcome: "completed",
    reply: { blocks: [{ type: "text", text: "唯一最终回复" }] },
  });
  await expect(chat.getByText("唯一最终回复", { exact: true })).toHaveCount(1);
});

test("deltas_dropped withdraws text until every affected Attempt closes", async ({
  page,
}) => {
  const session = await controlledTransport(page);
  await emit(
    page,
    "state_patch",
    state(session, 1, {
      coordinator_state: "running",
      active_run: run(session),
    }),
  );
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  await domain(
    page,
    1,
    session,
    { name: "attempt.started", attempt_id: "a" },
    { attempt: "a", step: 0 },
  );
  await domain(
    page,
    2,
    session,
    { name: "block.started", index: 0, block_type: "text" },
    { attempt: "a", step: 0 },
  );
  await domain(
    page,
    3,
    session,
    { name: "block.delta", index: 0, text: "将被撤回" },
    { attempt: "a", step: 0 },
  );
  const chat = page.getByRole("region", { name: "主对话" });
  await expect(chat).toContainText("将被撤回");
  await emit(page, "transport_notice", { code: "deltas_dropped", count: 1 });
  await expect(chat).not.toContainText("将被撤回");
  const notice = page.getByRole("complementary", { name: "本标签页连接状态" });
  await expect(notice).toContainText("部分增量未收到");
  await notice.getByRole("button", { name: "关闭连接提示" }).click();
  await domain(
    page,
    4,
    session,
    { name: "block.delta", index: 0, text: "不应恢复" },
    { attempt: "a", step: 0 },
  );
  await domain(
    page,
    5,
    session,
    { name: "attempt.committed", blocks: [{ type: "text", text: "快照" }] },
    { attempt: "a", step: 0 },
  );
  await expect(chat).not.toContainText("不应恢复");
  await expect(notice).toBeHidden();
});

test("duplicate, regressing and foreign patches do not replace authoritative state", async ({
  page,
}) => {
  const session = await controlledTransport(page);
  await emit(
    page,
    "state_patch",
    state(session, 5, {
      coordinator_state: "running",
      active_run: run(session),
    }),
  );
  const card = page.getByRole("region", { name: "当前运行" });
  await expect(card).toContainText("运行中");
  for (const patch of [
    state(session, 5),
    state(session, 4),
    state(session, 9, { process_instance_id: "another-process" }),
  ])
    await emit(page, "state_patch", patch);
  await expect(card).toContainText("运行中");
  await expect(
    page.getByRole("button", { name: "发送", exact: true }),
  ).toBeDisabled();
});

test("a terminal Attempt snapshot settles the affected dropped-delta notice without inventing events", async ({
  page,
}) => {
  const session = await controlledTransport(page);
  await emit(
    page,
    "state_patch",
    state(session, 1, {
      coordinator_state: "running",
      active_run: run(session, { current_step: 0 }),
      step: { step_index: 0, attempts: [] },
    }),
  );
  await domain(
    page,
    1,
    session,
    { name: "attempt.started", attempt_id: "a" },
    { attempt: "a", step: 0 },
  );
  await emit(page, "transport_notice", { code: "deltas_dropped", count: 1 });
  const notice = page.getByRole("complementary", { name: "本标签页连接状态" });
  await expect(notice).toBeVisible();
  await emit(
    page,
    "state_patch",
    state(session, 2, {
      coordinator_state: "running",
      active_run: run(session, { current_step: 0 }),
      step: {
        step_index: 0,
        attempts: [
          {
            attempt_id: "a",
            outcome: "aborted",
            duration_ms: 1,
            error_code: null,
            stop_reason: null,
          },
        ],
        attempts_truncated: false,
      },
    }),
  );
  await expect(notice).toBeHidden();
});

test("an unrecorded terminal snapshot recovers full text and refuses stale pending", async ({
  page,
}) => {
  const session = await controlledTransport(page);
  await page.route("**/api/reply?*", (route) => {
    const query = Object.fromEntries(
      new URL(route.request().url()).searchParams,
    );
    return route.fulfill({
      json: { ...query, reply_text: "完整但尚未保存的回复" },
    });
  });
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  const terminal = run(session, {
    phase: "finished",
    outcome: "completed",
    recording_state: "pending",
  });
  const projection = {
    run_id: "r1",
    session_id: session,
    purpose: "chat",
    outcome: "completed",
    reply_preview: "完整但…",
    prompt_preview: "问题",
    error: null,
    recording_state: "pending",
  };
  await emit(
    page,
    "state_patch",
    state(session, 1, {
      coordinator_state: "recording_pending",
      active_run: terminal,
      recording_state: "pending",
      unrecorded_terminal_projection: projection,
    }),
  );
  const chat = page.getByRole("region", { name: "主对话" });
  await expect(chat).toContainText("完整但尚未保存的回复");
  await expect(chat).toContainText("正在保存");
  await emit(
    page,
    "state_patch",
    state(session, 2, {
      coordinator_state: "recording_failed",
      active_run: { ...terminal, recording_state: "failed" },
      recording_state: "failed",
      unrecorded_terminal_projection: {
        ...projection,
        recording_state: "failed",
      },
    }),
  );
  await expect(chat).toContainText("回复已收到但未保存");
  await expect(page.getByRole("alert")).toHaveText("回复已收到但未保存");
  await emit(
    page,
    "state_patch",
    state(session, 3, {
      coordinator_state: "recording_pending",
      active_run: terminal,
      recording_state: "pending",
      unrecorded_terminal_projection: projection,
    }),
  );
  await expect(chat).toContainText("回复已收到但未保存");
  await expect(chat).not.toContainText("正在保存");
});
