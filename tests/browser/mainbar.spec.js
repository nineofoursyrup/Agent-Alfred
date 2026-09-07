import { test, expect } from "@playwright/test";

test("MainBar keeps its draft and drawer across pages and a refresh", async ({
  page,
}) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Gateway 收件箱" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  const input = page.getByRole("textbox", { name: "消息" });
  await input.fill("跨页保留这份草稿");
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  await expect(page.getByRole("region", { name: "主对话" })).toBeVisible();
  await page.getByRole("link", { name: "运行", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "运行详情", exact: true }),
  ).toBeVisible();
  await expect(input).toHaveValue("跨页保留这份草稿");
  await expect(page.getByRole("region", { name: "主对话" })).toBeVisible();
  await page.reload();
  await expect(input).toHaveValue("跨页保留这份草稿");
  await expect(page.getByRole("region", { name: "主对话" })).toBeVisible();
  await input.press("Escape");
  await expect(page.getByRole("region", { name: "主对话" })).toBeHidden();
  await expect(input).toHaveValue("跨页保留这份草稿");
});

test("an accepted message receives one recorded reply, also after refresh", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await page.getByRole("button", { name: "展开对话", exact: true }).click();
  const input = page.getByRole("textbox", { name: "消息" });
  await input.fill("请回复");
  // Session hydration and SSE readiness are asynchronous after creation.
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
  const accepted = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/runs") &&
      response.request().method() === "POST",
  );
  await input.press("Enter");
  expect((await accepted).status()).toBe(202);
  const conversation = page.getByRole("region", { name: "主对话" });
  await expect(
    conversation.getByText("离线模型回复", { exact: true }),
  ).toHaveCount(1);
  await expect(conversation.getByText("已保存", { exact: true })).toBeVisible();
  await expect(input).toHaveValue("");
  await expect(input).toBeFocused();
  await page.reload();
  await expect(
    conversation.getByText("离线模型回复", { exact: true }),
  ).toHaveCount(1);
  await expect(conversation.getByText("请回复", { exact: true })).toHaveCount(
    1,
  );
});
