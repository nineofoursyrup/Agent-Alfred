import { test, expect } from "@playwright/test";

test("Models and Connections navigation keeps MainBar", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("link", { name: "模型", exact: true }).click();
  await expect(page.getByRole("heading", { name: "模型", exact: true })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
  await page.getByRole("link", { name: "连接", exact: true }).click();
  await expect(page.getByRole("heading", { name: "连接", exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "消息" })).toBeVisible();
  await expect(page.getByText("无免费认证探针").first()).toBeVisible();
});

test("unsupported grok rows do not show 支持", async ({ page }) => {
  await page.goto("/models");
  await expect(page.getByRole("heading", { name: "模型", exact: true })).toBeVisible();
  const grok = page.locator("[data-model$=':grok-4.6']");
  await expect.poll(async () => grok.count()).toBeGreaterThan(0);
  for (const row of await grok.all()) {
    await expect(row.locator("[data-dimension=support]")).not.toContainText("支持");
  }
  const assigned = page.locator("[data-model='opencode-go:deepseek-v4-flash']");
  await expect(assigned.getByRole("button", { name: "取消钉选" })).toBeDisabled();
  await expect(assigned.getByRole("combobox", { name: "线路形状" })).toBeVisible();
  await expect(assigned.getByRole("textbox", { name: "显示名" })).toBeVisible();
  await expect(assigned.getByRole("button", { name: "指派为检索门" })).toBeVisible();
  await expect(
    page.locator("[data-endpoint=opencode-go] [data-dimension=connection]"),
  ).toBeVisible();
});

test("auth probe button posts only endpoint_id, shows result, and restores", async ({
  page,
}) => {
  /** @type {Record<string, unknown>[]} */
  const posts = [];
  let releaseProbe;
  const held = new Promise((resolve) => {
    releaseProbe = resolve;
  });
  const connected = {
    endpoints: [
      {
        endpoint_id: "openai",
        base_url: "https://api.openai.com/v1",
        catalog_url: null,
        api_key_env: "OPENAI_API_KEY",
        key: { configured: true, last4: "efgh", masked: false },
        observation: {
          state: "connected",
          checked_at: "2026-08-28T12:00:00Z",
          checked_via: "auth_probe",
          reason: null,
        },
        catalog: {
          health: "unfetched",
          last_success_at: null,
          last_error: null,
          retry_at: null,
        },
        auth_probe: { available: true, label: null },
      },
    ],
  };
  await page.route("**/api/connections/probe", async (route) => {
    posts.push(JSON.parse(route.request().postData() || "{}"));
    await held;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(connected),
    });
  });
  await page.route("**/api/connections", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/connections" && route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          endpoints: [
            {
              endpoint_id: "openai",
              base_url: "https://api.openai.com/v1",
              catalog_url: null,
              api_key_env: "OPENAI_API_KEY",
              key: { configured: true, last4: "efgh", masked: false },
              observation: {
                state: "configured_untested",
                checked_at: null,
                checked_via: null,
                reason: null,
              },
              catalog: {
                health: "unfetched",
                last_success_at: null,
                last_error: null,
                retry_at: null,
              },
              auth_probe: { available: true, label: null },
            },
          ],
        }),
      });
      return;
    }
    await route.continue();
  });
  await page.goto("/connections");
  const button = page.getByRole("button", { name: "验证凭据" });
  await expect(button).toBeEnabled();
  await button.click();
  await expect(button).toBeDisabled();
  releaseProbe();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toEqual({ endpoint_id: "openai" });
  await expect(page.getByText("已连接")).toBeVisible();
  await expect(page.getByText("auth_probe")).toBeVisible();
  await expect(page.getByRole("button", { name: "验证凭据" })).toBeEnabled();
});

test("auth probe error restores the button and shows the machine code", async ({
  page,
}) => {
  await page.route("**/api/connections/probe", async (route) => {
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ code: "mutation_in_flight" }),
    });
  });
  await page.route("**/api/connections", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/connections" && route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          endpoints: [
            {
              endpoint_id: "openai",
              base_url: "https://api.openai.com/v1",
              catalog_url: null,
              api_key_env: "OPENAI_API_KEY",
              key: { configured: true, last4: "efgh", masked: false },
              observation: {
                state: "configured_untested",
                checked_at: null,
                checked_via: null,
                reason: null,
              },
              catalog: {
                health: "unfetched",
                last_success_at: null,
                last_error: null,
                retry_at: null,
              },
              auth_probe: { available: true, label: null },
            },
          ],
        }),
      });
      return;
    }
    await route.continue();
  });
  await page.goto("/connections");
  const button = page.getByRole("button", { name: "验证凭据" });
  await button.click();
  await expect(page.getByText("mutation_in_flight")).toBeVisible();
  await expect(button).toBeEnabled();
});

test("assigned inference probe names the row and catalog refresh exists", async ({
  page,
}) => {
  /** @type {Record<string, unknown>[]} */
  const posts = [];
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() === "POST") {
      posts.push(JSON.parse(route.request().postData() || "{}"));
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ code: "endpoint_unconfigured" }),
      });
      return;
    }
    await route.continue();
  });
  await page.goto("/models");
  const assigned = page.locator("[data-model='opencode-go:deepseek-v4-flash']");
  const probe = assigned.getByRole("button", { name: "测试真实调用" });
  await expect(probe).toBeVisible();
  await expect(assigned.getByText("可能产生费用")).toBeVisible();
  await expect(page.getByRole("button", { name: "刷新目录" }).first()).toBeVisible();
  await expect(
    page.locator("[data-endpoint=opencode-go] [data-dimension=catalog]"),
  ).toBeVisible();
  if (await probe.isEnabled()) {
    await probe.click();
    await expect.poll(() => posts.length).toBe(1);
    expect(posts[0]).toMatchObject({
      purpose: "inference_probe",
      endpoint_id: "opencode-go",
      model_id: "deepseek-v4-flash",
    });
  } else {
    await expect(probe).toBeDisabled();
  }
});
