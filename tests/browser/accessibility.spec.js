import { test, expect } from "@playwright/test";

test("the narrow drawer is a keyboard-contained dialog that restores focus", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  const input = page.getByRole("textbox", { name: "消息" });
  await input.fill("中文草稿");
  const opener = page.getByRole("button", { name: "展开对话", exact: true });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "主对话对话框" });
  await expect(dialog).toHaveAttribute("aria-modal", "true");
  await expect(input).toBeFocused();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  expect(
    await page.evaluate(
      () => document.activeElement.closest('[role="dialog"]') !== null,
    ),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
  await expect(input).toHaveValue("中文草稿");
});

test("composition Enter does not send, Shift Enter inserts a newline, normal Enter sends once", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  const input = page.getByRole("textbox", { name: "消息" });
  await input.fill("中文输入");
  const submissions = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/runs"))
      submissions.push(request);
  });
  await input.dispatchEvent("keydown", {
    key: "Enter",
    isComposing: true,
    keyCode: 229,
  });
  await input.press("Shift+Enter");
  await expect(input).toHaveValue("中文输入\n");
  expect(submissions).toHaveLength(0);
  const accepted = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/runs") && response.status() === 202,
  );
  await input.press("Enter");
  await accepted;
  expect(submissions).toHaveLength(1);
});
