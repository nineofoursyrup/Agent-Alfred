import { defineConfig } from "@playwright/test";

const port = Number(process.env.ALFRED_BROWSER_TEST_PORT || 17736);
export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 15000,
  expect: { timeout: 4000 },
  outputDir: "output/playwright/results",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    browserName: "chromium",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `.venv/bin/python tests/browser/server.py --port ${port}`,
    url: `http://127.0.0.1:${port}/api/entry`,
    reuseExistingServer: false,
  },
});
