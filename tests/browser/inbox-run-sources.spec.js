import { test, expect } from "@playwright/test";
import { localServer } from "./local-server.js";

function seed(length) {
  return `
import sqlite3,json,sys
from pathlib import Path
from agent_alfred import schema
conn=sqlite3.connect(Path(sys.argv[1])/'db.sqlite3')
schema.migrate(conn)
# Retain a future-purpose historic row to exercise the production unknown case.
conn.execute('PRAGMA ignore_check_constraints=ON')
for session in ['', '历史 /?会话#']:
 conn.execute('INSERT INTO sessions(session_id,created_at,activity_revision) VALUES (?,?,?)',(session,'2026-10-01T00:00:00Z',schema.allocate_activity_revision(conn)))
 for index in range(${length}):
  identity=('' if session=='' else '特殊 /?运行#') if index==0 else session+'run-'+str(index)
  conn.execute("INSERT INTO runs(run_id,purpose,session_id,gateway,prompt_preview,phase,outcome,accepted_at,started_at,finished_at,activity_revision,admission_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(identity,'aggregation' if session=='' else 'chat',session,'cli','安全请求 '+str(index),'finished','completed','2026-10-01T00:00:00Z','2026-10-01T00:00:01Z','2026-10-01T00:00:02Z',schema.allocate_activity_revision(conn),'admitted'))
  for role in ['user','assistant']:
   conn.execute('INSERT INTO agent_log(session_id,role,content,source,created_at,run_id) VALUES (?,?,?,?,?,?)',(session,role,json.dumps([{'type':'text','text':'相同文字'}]),'cli','2026-10-01T00:00:02Z',identity))
 for index in range(3):
  conn.execute('INSERT INTO agent_log(session_id,role,content,source,created_at) VALUES (?,?,?,?,?)',(session,'user',json.dumps([{'type':'text','text':'同文历史'}]),'cli','old'))
for index in range(${length}):
 conn.execute('INSERT INTO sessions(session_id,created_at,activity_revision) VALUES (?,?,?)',('padding-'+str(index),'2026-10-01T00:00:00Z',schema.allocate_activity_revision(conn)))
for purpose,admission in [('chat','rejected'),('chat','pending'),('chat','unconfirmed'),('aggregation','admitted'),('future-purpose','admitted')]:
 conn.execute("INSERT INTO runs(run_id,purpose,gateway,prompt_preview,phase,outcome,accepted_at,started_at,finished_at,activity_revision,admission_state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",(purpose+'-'+admission,purpose,'cli','DO_NOT_DISCLOSE','finished','failed','2026-10-01T00:00:00Z','2026-10-01T00:00:01Z','2026-10-01T00:00:02Z',schema.allocate_activity_revision(conn),admission))
conn.commit();conn.close()
`;
}

for (const length of [4, 60])
  test(`real bounded sources preserve empty and special identities across ${length} Run histories`, async ({
    page,
  }) => {
    const server = await localServer(seed(length));
    try {
      const reads = [];
      page.on("request", (r) => {
        if (
          r.method() === "GET" &&
          new URL(r.url()).pathname.startsWith("/api/")
        )
          reads.push(r.url());
      });
      for (const [session, run] of [
        ["", ""],
        ["历史 /?会话#", "特殊 /?运行#"],
      ]) {
        await page.goto(
          server.origin +
            "/inbox?" +
            new URLSearchParams({ session_id: session, view: "messages" }),
        );
        const preview = page.getByRole("region", { name: "会话只读预览" });
        await expect(preview).toContainText("相同文字");
        const links = preview.getByRole("link", {
          name: "查看运行",
          exact: true,
        });
        const target = links
          .filter({ hasText: "查看运行" })
          .and(
            preview.locator(
              'a[href="/runs/' + encodeURIComponent(run) + '?filter=chat"]',
            ),
          );
        // Session messages are chronological inside the Run segment. Equal text
        // remains distinct because the source anchor, not text, is the identity.
        await expect(target).toHaveCount(2);
        await target.last().click();
        await expect(
          page.getByRole("region", { name: "运行摘要" }),
        ).toContainText("记录状态未知");
        await expect(
          page.getByRole("region", { name: "追踪导出" }),
        ).toBeVisible();
        const replyStart = reads.length;
        await page.getByRole("button", { name: /在主对话中查看$/ }).click();
        const reply = page.locator('#messages [data-run-id="' + run + '"]');
        await expect(reply).toHaveCount(1);
        await expect(reply).toContainText("相同文字");
        await expect(page.locator("#reading-status")).toContainText(
          "相邻历史尚未读取",
        );
        const replyReads = reads
          .slice(replyStart)
          .filter((url) => new URL(url).pathname === "/api/mainbar/locate");
        expect(replyReads).toHaveLength(1);
        expect(new URL(replyReads[0]).searchParams.get("session_id")).toBe(
          session,
        );
        expect(new URL(replyReads[0]).searchParams.get("run_id")).toBe(run);
        // Cross-Session selection may read one normal history window; it never
        // walks historical pages to find this exact early reply.
        expect(
          reads
            .slice(replyStart)
            .filter((url) => new URL(url).pathname === "/api/sessions/messages")
            .length,
        ).toBeLessThanOrEqual(1);
        const start = reads.length;
        await page.getByRole("link", { name: "返回来源", exact: true }).click();
        await expect(
          preview.locator('[data-source-highlighted="true"]'),
        ).toHaveCount(1);
        await expect(
          preview
            .locator('[data-source-highlighted="true"]')
            .getByRole("link", { name: "查看运行", exact: true }),
        ).toBeFocused();
        const location = reads
          .slice(start)
          .filter(
            (url) => new URL(url).pathname === "/api/sessions/messages/locate",
          );
        expect(location).toHaveLength(1);
        expect(
          reads
            .slice(start)
            .filter(
              (url) => new URL(url).pathname === "/api/sessions/messages",
            ),
        ).toHaveLength(0);
        expect(new URL(location[0]).searchParams.get("session_id")).toBe(
          session,
        );
        await preview.getByRole("link", { name: "运行", exact: true }).click();
        await expect(preview.locator("[data-source-id]")).toHaveCount(
          Math.min(length, 25),
        );
        for (let loaded = 25; loaded < length; loaded += 25) {
          await preview
            .getByRole("button", { name: "更多会话运行", exact: true })
            .click();
          await expect(preview.locator("[data-source-id]")).toHaveCount(
            Math.min(loaded + 25, length),
          );
        }
        const runLink = preview.locator(
          'a[href="/runs/' + encodeURIComponent(run) + '?filter=chat"]',
        );
        await expect(runLink).toHaveCount(1);
        await runLink.click();
        const runStart = reads.length;
        await page.getByRole("link", { name: "返回来源", exact: true }).click();
        await expect(
          preview.locator('[data-source-highlighted="true"]'),
        ).toHaveCount(1);
        await expect(
          preview
            .locator('[data-source-highlighted="true"]')
            .getByRole("link", { name: "查看运行", exact: true }),
        ).toBeFocused();
        expect(
          reads
            .slice(runStart)
            .filter(
              (url) => new URL(url).pathname === "/api/sessions/runs/locate",
            ),
        ).toHaveLength(1);
        await page
          .getByRole("link", { name: "返回会话列表", exact: true })
          .click();
        await expect(
          page
            .getByRole("region", { name: "会话列表" })
            .locator('[data-source-highlighted="true"]'),
        ).toHaveAttribute("data-source-id", session);
        await expect(
          page
            .getByRole("region", { name: "会话列表" })
            .locator('[data-source-highlighted="true"]')
            .getByRole("link"),
        ).toBeFocused();
      }
    } finally {
      await server.close();
    }
  });

test("real summary privacy gates and container 679/680 use one equivalent table/card DOM", async ({
  page,
}) => {
  const server = await localServer(seed(4));
  try {
    const bodies = [];
    page.on("response", async (response) => {
      if (
        new URL(response.url()).pathname === "/api/runs" &&
        response.request().method() === "GET"
      )
        bodies.push(await response.json());
    });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(server.origin + "/runs");
    const list = page.getByRole("region", { name: "运行列表" });
    await expect(list).toContainText("future-purpose");
    expect(bodies).toHaveLength(1);
    for (const row of bodies[0].runs)
      if (row.admission_state !== "admitted" || row.purpose !== "chat")
        expect(row.prompt_preview).toBeNull();
    await expect(list).not.toContainText("DO_NOT_DISCLOSE");
    const keys = await list
      .locator("[data-source-id]")
      .evaluateAll((rows) =>
        rows.map((row) => [
          row.getAttribute("data-source-id"),
          row.textContent,
        ]),
      );
    await page.getByRole("button", { name: "收起主对话", exact: true }).click();
    for (const width of [679, 680]) {
      await list.evaluate((element, width) => {
        element.style.width = width + "px";
        element.style.maxWidth = "none";
      }, width);
      expect(
        await list.evaluate((element) => element.getBoundingClientRect().width),
      ).toBe(width);
      expect(
        await list
          .locator("table")
          .evaluate((element) => getComputedStyle(element).display),
      ).toBe(width === 679 ? "block" : "table");
      expect(
        await list
          .locator("[data-source-id]")
          .evaluateAll((rows) =>
            rows.map((row) => [
              row.getAttribute("data-source-id"),
              row.textContent,
            ]),
          ),
      ).toEqual(keys);
      await expect(list.getByRole("link", { name: "查看运行" })).toHaveCount(
        keys.length,
      );
    }
    await list.evaluate((element) => element.removeAttribute("style"));
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
    }
    await list
      .getByRole("link", { name: "查看运行", exact: true })
      .first()
      .click();
    const fields = page.getByRole("region", { name: "运行摘要" }).locator("dt");
    for (const width of [1440, 320]) {
      await page.setViewportSize({ width, height: 900 });
      await expect(fields.first()).toBeVisible();
      await expect
        .poll(() =>
          fields.evaluateAll((items) => [
            ...new Set(items.map((item) => getComputedStyle(item).fontSize)),
          ]),
        )
        .toEqual(["11px"]);
    }
  } finally {
    await server.close();
  }
});

test("all Runs return keeps its source filter through one bounded read", async ({
  page,
}) => {
  const server = await localServer(seed(60));
  try {
    const reads = [];
    page.on("request", (request) => {
      if (request.method() === "GET") reads.push(request.url());
    });
    await page.goto(server.origin + "/runs?filter=all");
    const list = page.getByRole("region", { name: "运行列表" });
    const target = list.locator('a[href="/runs/?filter=all"]');
    await expect(list.locator("[data-source-id]")).toHaveCount(25);
    for (let loaded = 25; loaded < 125; loaded += 25) {
      await list.getByRole("button", { name: "更多运行", exact: true }).click();
      await expect(list.locator("[data-source-id]")).toHaveCount(loaded + 25);
    }
    await target.click();
    await expect(page.getByRole("region", { name: "运行摘要" })).toContainText(
      "aggregation",
    );
    const start = reads.length;
    await page.getByRole("link", { name: "返回来源", exact: true }).click();
    await expect(
      list.locator('[data-source-highlighted="true"]'),
    ).toHaveAttribute("data-source-id", "");
    await expect(target).toBeFocused();
    await expect(page.getByRole("combobox", { name: "运行筛选" })).toHaveValue(
      "all",
    );
    const locations = reads
      .slice(start)
      .filter((url) => new URL(url).pathname === "/api/runs/locate/");
    expect(locations).toHaveLength(1);
    expect(new URL(locations[0]).searchParams.get("filter")).toBe("all");
    expect(
      reads.slice(start).filter((url) => new URL(url).pathname === "/api/runs"),
    ).toHaveLength(0);
  } finally {
    await server.close();
  }
});

test("real detail snapshots expire without polling and disclose a disconnected source", async ({
  page,
}) => {
  const server = await localServer(seed(4));
  let stopped = false;
  try {
    await page.clock.install();
    const reads = [];
    page.on("request", (request) => {
      if (
        ["/api/run-evidence", "/api/runs/locate/"].includes(
          new URL(request.url()).pathname,
        )
      )
        reads.push(request.url());
    });
    await page.goto(server.origin + "/runs/");
    const statuses = page.locator(".page-body > [role=status]");
    await expect(
      statuses.filter({ hasText: "过程证据共同来源 · 读取于" }),
    ).toHaveCount(1);
    const count = reads.length;
    await page.clock.fastForward(16 * 60 * 1000);
    await expect(statuses.filter({ hasText: "已过期，请刷新" })).toHaveCount(2);
    expect(reads.length).toBe(count);
    await server.close();
    stopped = true;
    await expect(
      statuses.filter({ hasText: "连接中断，保留旧快照" }),
    ).toHaveCount(2);
    await expect(page.getByRole("region", { name: "运行摘要" })).toContainText(
      "aggregation",
    );
  } finally {
    if (!stopped) await server.close();
  }
});
