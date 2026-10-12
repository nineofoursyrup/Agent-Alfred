# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: skills.spec.js >> CE-23: a late real evidence response cannot replace the next Run
- Location: tests/browser/skills.spec.js:67:1

# Error details

```
Error: locator.click: Error: strict mode violation: locator('a[href^="/runs/fa99a1ec1a544cfe8ee2c1e2b6414d8c"]') resolved to 2 elements:
    1) <a data-source-trigger="open-run" href="/runs/fa99a1ec1a544cfe8ee2c1e2b6414d8c?filter=all">查看运行</a> aka getByRole('link', { name: '查看运行' }).first()
    2) <a class="message-run-link" href="/runs/fa99a1ec1a544cfe8ee2c1e2b6414d8c">查看运行</a> aka getByRole('link', { name: '查看运行' }).nth(3)

Call log:
  - waiting for locator('a[href^="/runs/fa99a1ec1a544cfe8ee2c1e2b6414d8c"]')

```

# Page snapshot

```yaml
- generic [ref=f1e1]:
  - banner "导航面板" [ref=f1e2]:
    - generic [ref=f1e3]:
      - link "Alfred 本地助手" [ref=f1e4] [cursor=pointer]:
        - /url: /overview
        - text: Alfred
        - generic [ref=f1e5]: 本地助手
      - paragraph [ref=f1e6]:
        - generic [ref=f1e8]: 连接中断或正在同步
      - paragraph [ref=f1e9]: 127.0.0.1:18034
    - navigation "主导航" [ref=f1e10]:
      - paragraph [ref=f1e11]: 工作区
      - link "总览" [ref=f1e12] [cursor=pointer]:
        - /url: /overview
      - link "收件箱" [ref=f1e13] [cursor=pointer]:
        - /url: /inbox
      - link "运行" [ref=f1e14] [cursor=pointer]:
        - /url: /runs
      - link "记忆" [ref=f1e15] [cursor=pointer]:
        - /url: /memory
      - paragraph [ref=f1e16]: Agent 配置
      - link "模型" [ref=f1e17] [cursor=pointer]:
        - /url: /models
      - link "连接" [ref=f1e18] [cursor=pointer]:
        - /url: /connections
      - link "行为" [ref=f1e19] [cursor=pointer]:
        - /url: /behaviour
      - link "工具" [ref=f1e20] [cursor=pointer]:
        - /url: /tools
      - paragraph [ref=f1e21]: 诊断
      - link "用量账本" [ref=f1e22] [cursor=pointer]:
        - /url: /ops
      - link "数据库" [ref=f1e23] [cursor=pointer]:
        - /url: /database
    - paragraph [ref=f1e24]: 本地优先 · 共享运行宿主
  - main [ref=f1e26]:
    - generic [ref=f1e27]:
      - generic [ref=f1e28]:
        - paragraph [ref=f1e29]: RUNS
        - heading "运行" [active] [level=1] [ref=f1e30]
        - paragraph [ref=f1e31]: 查看会话运行与系统运行的过程、来源和用量证据。
      - generic "壳层状态与操作" [ref=f1e32]:
        - button "打开主对话" [expanded] [ref=f1e33] [cursor=pointer]
        - region "运行与连接摘要" [ref=f1e34]:
          - paragraph [ref=f1e35]:
            - generic [ref=f1e37]: 连接中断或正在同步
    - region "运行列表" [ref=f1e39]:
      - generic [ref=f1e40]:
        - heading "运行记录" [level=2] [ref=f1e41]
        - combobox "运行筛选" [ref=f1e42]:
          - option "全部" [selected]
          - option "会话运行"
          - option "系统运行"
      - table "运行记录" [ref=f1e44]:
        - rowgroup
        - rowgroup [ref=f1e45]:
          - row [ref=f1e46]:
            - cell [ref=f1e47]:
              - generic [ref=f1e49]:
                - term [ref=f1e50]: 输入预览
                - definition [ref=f1e51]: /skills off B run disabled
                - button "展开预览" [ref=f1e52] [cursor=pointer]
              - generic [ref=f1e53]:
                - generic [ref=f1e54]:
                  - term [ref=f1e55]: 结果
                  - definition [ref=f1e56]: 回复完成
                - generic [ref=f1e57]:
                  - term [ref=f1e58]: 记录
                  - definition [ref=f1e59]: 已保存
                - generic [ref=f1e60]:
                  - term [ref=f1e61]: 耗时
                  - definition [ref=f1e62]: <1 s
              - generic [ref=f1e63]:
                - generic [ref=f1e64]:
                  - term [ref=f1e65]: Run ID
                  - definition [ref=f1e66]: fa99a1ec1a544cfe8ee2c1e2b6414d8c
                - generic [ref=f1e67]:
                  - term [ref=f1e68]: 用途
                  - definition [ref=f1e69]: 普通聊天（chat）
                - generic [ref=f1e70]:
                  - term [ref=f1e71]: 所属分组
                  - definition [ref=f1e72]: 会话运行
              - group [ref=f1e73]:
                - generic "运行记录明细" [ref=f1e74]
            - cell [ref=f1e75]:
              - link "查看运行" [ref=f1e76] [cursor=pointer]:
                - /url: /runs/fa99a1ec1a544cfe8ee2c1e2b6414d8c?filter=all
          - row [ref=f1e77]:
            - cell [ref=f1e78]:
              - generic [ref=f1e80]:
                - term [ref=f1e81]: 输入预览
                - definition [ref=f1e82]: A run automatic
                - button "展开预览" [ref=f1e83] [cursor=pointer]
              - generic [ref=f1e84]:
                - generic [ref=f1e85]:
                  - term [ref=f1e86]: 结果
                  - definition [ref=f1e87]: 回复完成
                - generic [ref=f1e88]:
                  - term [ref=f1e89]: 记录
                  - definition [ref=f1e90]: 已保存
                - generic [ref=f1e91]:
                  - term [ref=f1e92]: 耗时
                  - definition [ref=f1e93]: <1 s
              - generic [ref=f1e94]:
                - generic [ref=f1e95]:
                  - term [ref=f1e96]: Run ID
                  - definition [ref=f1e97]: 2b8765de3b434d07a0ba16e3c69b87f0
                - generic [ref=f1e98]:
                  - term [ref=f1e99]: 用途
                  - definition [ref=f1e100]: 普通聊天（chat）
                - generic [ref=f1e101]:
                  - term [ref=f1e102]: 所属分组
                  - definition [ref=f1e103]: 会话运行
              - group [ref=f1e104]:
                - generic "运行记录明细" [ref=f1e105]
            - cell [ref=f1e106]:
              - link "查看运行" [ref=f1e107] [cursor=pointer]:
                - /url: /runs/2b8765de3b434d07a0ba16e3c69b87f0?filter=all
      - generic [ref=f1e108]:
        - status [ref=f1e109]: 读取于 2:13:52 PM · 连接中断，保留旧快照
        - button "刷新运行" [disabled] [ref=f1e110]
  - region "MainBar" [ref=f1e111]:
    - generic [ref=f1e112]:
      - heading "主对话" [level=2] [ref=f1e113]
      - generic [ref=f1e114]: 未同步
      - button "收起主对话" [ref=f1e117] [cursor=pointer]
    - complementary "本标签页连接状态" [ref=f1e118]:
      - paragraph [ref=f1e119]: 连接已断开；已清除临时文字，正在重新连接。
      - text: 仅影响本标签页
      - button "关闭连接提示" [ref=f1e120] [cursor=pointer]
    - generic [ref=f1e121]:
      - generic [ref=f1e122]: 会话
      - button "新建会话" [ref=f1e123] [cursor=pointer]
    - region "主对话" [ref=f1e124]:
      - generic [ref=f1e125]:
        - article [ref=f1e126]:
          - paragraph [ref=f1e127]: A run automatic
          - paragraph [ref=f1e128]: 离线 Skill 回复
          - paragraph [ref=f1e129]: Skill 已降级：部分流程未使用或选择器不可用；请查看运行详情中的本次输入。
          - generic [ref=f1e130]: 已保存
          - link "查看运行" [ref=f1e131] [cursor=pointer]:
            - /url: /runs/2b8765de3b434d07a0ba16e3c69b87f0
        - article [ref=f1e132]:
          - paragraph [ref=f1e133]: /skills off B run disabled
          - paragraph [ref=f1e134]: 离线 Skill 回复
          - generic [ref=f1e135]: 已保存
          - link "查看运行" [ref=f1e136] [cursor=pointer]:
            - /url: /runs/fa99a1ec1a544cfe8ee2c1e2b6414d8c
    - generic [ref=f1e137]:
      - generic [ref=f1e138]: 消息
      - textbox "消息" [ref=f1e139]:
        - /placeholder: 写下消息；/skills 指定流程…
      - button "发送" [disabled] [ref=f1e140]
    - paragraph [ref=f1e141]: 连接尚未同步；草稿仍可编辑。
  - status [ref=f1e142]: 就绪
  - alert [ref=f1e143]
```

# Test source

```ts
  1   | import {test, expect} from "@playwright/test";
  2   | import {mkdir, writeFile} from "node:fs/promises";
  3   | import {join} from "node:path";
  4   | import {api, memoryServer} from "./memory-server.js";
  5   |
  6   | async function prepare(directory) {
  7   |   for (const [name, body] of [["A", "历史 A 完整流程"], ["B", "长".repeat(8001)], ["C", "C 完整流程"]]) {
  8   |     const path = join(directory, "builtin", name);
  9   |     await mkdir(path, {recursive: true});
  10  |     await writeFile(join(path, "SKILL.md"), `---\nname: ${name}\ndescription: ${name}流程\n---\n${body}`);
  11  |   }
  12  | }
  13  | async function chat(page, origin) {
  14  |   await page.goto(origin + "/inbox");
  15  |   const [created] = await Promise.all([
  16  |     page.waitForResponse(r => r.url().endsWith("/api/sessions") && r.request().method() === "POST"),
  17  |     page.getByRole("button", {name: "新建会话", exact: true}).click(),
  18  |   ]);
  19  |   expect(created.status()).toBe(201);
  20  |   const session = (await created.json()).session_id;
  21  |   await page.waitForFunction(id => sessionStorage.getItem("alfred.session") === id, session);
  22  |   const expand = page.locator('#shell-toolbar [data-open-panel="mainbar"]');
  23  |   if (await expand.isVisible()) await expand.click();
  24  | }
  25  | async function send(page, text) {
  26  |   await page.getByRole("textbox", {name: "消息"}).fill(text);
  27  |   await page.getByRole("button", {name: "发送", exact: true}).click();
  28  | }
  29  | async function runFor(other, prompt) {
  30  |   let found;
  31  |   await expect.poll(async () => {
  32  |     const {status, body} = await other.get("/api/runs?filter=chat&limit=25");
  33  |     expect(status).toBe(200);
  34  |     found = body.runs.find(r => r.prompt_preview === prompt && r.phase === "finished");
  35  |     return found?.run_id;
  36  |   }).toBeTruthy();
  37  |   return found.run_id;
  38  | }
  39  |
  40  | test("CE-10/22/28: real Skill preparation, attempts, MainBar and historical metadata", async ({page}) => {
  41  |   const server = await memoryServer({script: "tests/browser/skills_server.py", prepare});
  42  |   try {
  43  |     const other = await api(page.request, server.origin);
  44  |     await chat(page, server.origin);
  45  |     await send(page, "自动整理");
  46  |     const run = await runFor(other, "自动整理");
  47  |     await expect(page.getByRole("region", {name: "主对话"}).getByText(/Skill 已降级/)).toBeVisible();
  48  |     await page.goto(`${server.origin}/runs/${run}`);
  49  |     const input = page.locator('details[data-attempt="input-explanation"]');
  50  |     await input.locator("summary").click();
  51  |     await expect(input.getByText(/选择方式：model/)).toBeVisible();
  52  |     await expect(input.getByText(/B.*body_limit_exceeded/)).toBeVisible();
  53  |     await expect(input.getByText(/实际请求携带 Skill A/)).toBeVisible();
  54  |     await expect(input.getByText(/实际请求携带 Skill C/)).toBeVisible();
  55  |     await writeFile(join(server.directory, "builtin", "A", "SKILL.md"), "---\nname: A\ndescription: 新版本\n---\n重启新正文");
  56  |     await server.restart();
  57  |     await page.reload();
  58  |     await input.locator("summary").click();
  59  |     await expect(input.getByText(/实际请求携带 Skill A/)).toBeVisible();
  60  |     await expect(input.getByText("重启新正文")).toHaveCount(0);
  61  |     await chat(page, server.origin);
  62  |     await send(page, "/skills Missing\n整理");
  63  |     await expect(page.getByRole("region", {name: "主对话"}).getByText(/Skill 准备失败/)).toBeVisible();
  64  |   } finally { await server.close(); }
  65  | });
  66  |
  67  | test("CE-23: a late real evidence response cannot replace the next Run", async ({page}) => {
  68  |   const server = await memoryServer({script: "tests/browser/skills_server.py", prepare});
  69  |   let release;
  70  |   const held = new Promise(resolve => {release = resolve;});
  71  |   let observed;
  72  |   const intercepted = new Promise(resolve => {observed = resolve;});
  73  |   let delivered;
  74  |   const complete = new Promise(resolve => {delivered = resolve;});
  75  |   try {
  76  |     const other = await api(page.request, server.origin);
  77  |     await chat(page, server.origin);
  78  |     await send(page, "A run automatic");
  79  |     const first = await runFor(other, "A run automatic");
  80  |     await send(page, "/skills off\nB run disabled");
  81  |     const second = await runFor(other, "/skills off\nB run disabled");
  82  |     await page.route(`**/api/run-evidence?run_id=${first}`, async route => {
  83  |       const response = await route.fetch();
  84  |       expect(response.status()).toBe(200);
  85  |       expect((await response.json()).run_id).toBe(first);
  86  |       observed();
  87  |       await held;
  88  |       await route.fulfill({response});
  89  |       delivered();
  90  |     });
  91  |     await page.goto(`${server.origin}/runs/${first}`, {waitUntil: "domcontentloaded"});
  92  |     await intercepted;
  93  |     await page.getByRole('link',{name:'返回运行列表',exact:true}).click();
> 94  |     await page.locator(`a[href^="/runs/${second}"]`).click();
      |                                                      ^ Error: locator.click: Error: strict mode violation: locator('a[href^="/runs/fa99a1ec1a544cfe8ee2c1e2b6414d8c"]') resolved to 2 elements:
  95  |     const input = page.locator('details[data-attempt="input-explanation"]');
  96  |     await input.locator("summary").click();
  97  |     await expect(input.getByText(/选择方式：disabled/)).toBeVisible();
  98  |     release();
  99  |     await complete;
  100 |     await expect(input.getByText(/选择方式：disabled/)).toBeVisible();
  101 |     await expect(input.getByText(/body_limit_exceeded/)).toHaveCount(0);
  102 |     await page.reload();
  103 |     await input.locator("summary").click();
  104 |     await expect(input.getByText(/选择方式：disabled/)).toBeVisible();
  105 |   } finally { release(); await server.close(); }
  106 | });
  107 |
  108 | test("CE-19/21: selector input failure has no fictitious Step or Attempt", async ({page}) => {
  109 |   const server = await memoryServer({script: "tests/browser/skills_server.py", prepare});
  110 |   try {
  111 |     await chat(page, server.origin);
  112 |     const accepted = page.waitForResponse(r => r.url().endsWith("/api/runs") && r.request().method() === "POST");
  113 |     await send(page, "x".repeat(64001));
  114 |     const response = await accepted;
  115 |     expect(response.status()).toBe(202);
  116 |     const {run_id} = await response.json();
  117 |     await expect(page.getByRole("region", {name: "主对话"})).toContainText("输入超限");
  118 |     await page.goto(`${server.origin}/runs/${run_id}`);
  119 |     const detail = page.getByRole("region", {name: "运行过程"});
  120 |     await detail.getByText("本次输入", {exact: true}).click();
  121 |     await expect(detail).toContainText("输入准备失败 · Skill 选择器");
  122 |     await expect(detail).not.toContainText("undefined");
  123 |     await expect(detail).not.toContainText("Attempt");
  124 |     await page.reload();
  125 |     await detail.getByText("本次输入", {exact: true}).click();
  126 |     await expect(detail).toContainText("输入准备失败 · Skill 选择器");
  127 |     await expect(detail).not.toContainText("undefined");
  128 |   } finally { await server.close(); }
  129 | });
  130 |
```
