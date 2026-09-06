import {test, expect} from "@playwright/test";
import {localServer} from "./local-server.js";

test("a real v3 migration labels only unconfirmed old Runs", async ({page}) => {
  const server = await localServer(`
import sqlite3, sys
from pathlib import Path
from agent_alfred import schema
conn = sqlite3.connect(Path(sys.argv[1]) / 'db.sqlite3')
schema.configure_connection(conn)
for migration in schema.MIGRATIONS:
    if migration.version > 3:
        break
    migration.apply(conn)
    conn.execute('INSERT INTO schema_migrations VALUES (?, ?)', (migration.version, 'original'))
for index, (run_id, started) in enumerate([('uncertain-old', None), ('started-old', 'start')], 1):
    conn.execute('''INSERT INTO runs (run_id, purpose, gateway, phase, outcome,
                    accepted_at, started_at, finished_at, activity_revision)
                    VALUES (?, 'chat', 'cli', 'finished', 'interrupted',
                            'accept', ?, 'finish', ?)''', (run_id, started, index))
conn.execute('UPDATE activity_clock SET next_revision = 3')
conn.commit()
conn.close()
`);
  try {
    await page.goto(`${server.origin}/runs?filter=all`);
    const response = await page.request.get(`${server.origin}/api/runs?filter=all`);
    expect(response.status()).toBe(200);
    const migrated = (await response.json()).runs;
    expect(migrated.find(run => run.run_id === "uncertain-old").admission_state).toBe("unconfirmed");
    expect(migrated.find(run => run.run_id === "started-old").admission_state).toBe("admitted");
    const rows = page.getByRole("region", {name:"运行列表"}).locator("article");
    const uncertain = rows.filter({has:page.locator('a[href*="uncertain-old"]')});
    const started = rows.filter({has:page.locator('a[href*="started-old"]')});
    await expect(uncertain).toContainText("准入未确认");
    await expect(uncertain).toContainText("执行前中断");
    await expect(started).not.toContainText("准入未确认");
    await expect(started).toContainText("运行终态无法确认");
    await uncertain.getByRole("link", {name:"查看运行"}).click();
    await expect(page.getByRole("region", {name:"运行列表"})).toContainText("准入未确认");
  } finally {
    await server.close();
  }
});
