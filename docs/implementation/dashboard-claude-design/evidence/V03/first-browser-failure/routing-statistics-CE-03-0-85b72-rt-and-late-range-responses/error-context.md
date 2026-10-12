# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: routing-statistics.spec.js >> CE-03/08/14 real recovery counts survive restart and late range responses
- Location: tests/browser/routing-statistics.spec.js:57:1

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: locator('.routing-statistics').getByText(/范围：24小时/)
Expected: visible
Timeout: 4000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" locator('.routing-statistics').getByText(/范围：24小时/) with timeout 4000ms
  - waiting for locator('.routing-statistics').getByText(/范围：24小时/)

```

```yaml
- banner "导航面板":
  - link "Alfred 本地助手":
    - /url: /overview
  - paragraph: 已同步
  - paragraph: 127.0.0.1:18034
  - navigation "主导航":
    - paragraph: 工作区
    - link "总览":
      - /url: /overview
    - link "收件箱":
      - /url: /inbox
    - link "运行":
      - /url: /runs
    - link "记忆":
      - /url: /memory
    - paragraph: Agent 配置
    - link "模型":
      - /url: /models
    - link "连接":
      - /url: /connections
    - link "行为":
      - /url: /behaviour
    - link "工具":
      - /url: /tools
    - paragraph: 诊断
    - link "用量账本":
      - /url: /ops
    - link "数据库":
      - /url: /database
  - paragraph: 本地优先 · 共享运行宿主
- main:
  - paragraph: BEHAVIOUR
  - heading "行为" [level=1]
  - paragraph: 查看与配置消息分流，区分流程拓扑、路由决策与执行事实。
  - button "打开主对话" [expanded]
  - region "运行与连接摘要":
    - paragraph: 已同步
  - region "消息分流":
    - heading "消息分流" [level=2]
    - paragraph: 持续设置 · CLI/Web 共享。默认关闭；保存后下一 Run 生效。回复进入当前会话，静默时只记用户消息，不生成助手消息或长期记忆。
    - paragraph: 已保存的分流设置：开启 · revision 1。能查看结构不代表已经启用或可以成功执行。
    - paragraph: 启用后识别纯问候、致谢及明确无需回复的消息；实际任务仍进入完整回答。
    - checkbox "启用消息分流" [checked]
    - text: 启用消息分流
    - paragraph: 当前选择：开启 · 编辑基线 revision 1。
    - button "保存设置"
    - button "刷新设置"
    - status: 当前值已核对；设置在下一 Run 生效。
    - region "流程拓扑":
      - button "查看流程"
      - status: 服务已连接；结构仅在手动读取时更新。
      - status: 尚未读取；展开后读取。
    - group:
      - text: 路由统计
      - status: 已读取24小时统计；快照 2026-10-12T06:05:31.135052+00:00。
      - paragraph: 跨会话 CLI/Web 已准入聊天 Run；按准入时刻归窗，仅启用且终态已持久保存的 Run 进入主结果。未知不代表未发生。
      - text: 统计范围
      - combobox "统计范围":
        - option "24小时" [selected]
        - option "7天"
        - option "30天"
        - option "全部"
      - button "刷新统计"
      - paragraph: 范围：24小时 · 10/11/2026, 2:05:31 PM（含）— 10/12/2026, 2:05:31 PM（不含）
      - paragraph: 显示时区：Asia/Shanghai；判窗 UTC。快照时间：2026-10-12T06:05:31.135052+00:00；读取身份：2e74413275c2479c90df4d5f5185b40c；进程：223c7d75b0794e5caa5b98f519b897be
      - paragraph: 已准入 1 · 启用 1 · 关闭 0 · 启用未知 0
      - paragraph: 启用样本：已持久终态 1 · 尚未记录终态 0；启用状态未知的已知图前绕行 0
      - paragraph: 路由决议不代表成功。组内可能包含不同模型、工具和参数；这些数字不表示分类准确率、成功率、费用节省或因果效果。
      - heading "当前版本" [level=3]
      - paragraph: 统计口径：routing-statistics-1；路由策略：message-routing-1
      - paragraph: 终态 1 · 已知决议 1 · 明确未形成决议 0 · 未知决议 0；决议覆盖 1/1 · 100%
      - list "路由决议比例":
        - listitem:
          - text: 快速回复：0 次；0/1 · 0%
          - progressbar "快速回复：0 次；0/1 · 0%"
        - listitem:
          - text: 完整回答：0 次；0/1 · 0%
          - progressbar "完整回答：0 次；0/1 · 0%"
        - listitem:
          - text: 图内保守兜底：0 次；0/1 · 0%
          - progressbar "图内保守兜底：0 次；0/1 · 0%"
        - listitem:
          - text: 不回复：1 次；1/1 · 100%
          - progressbar "不回复：1 次；1/1 · 100%"
        - listitem:
          - text: 上下文失败提示：0 次；0/1 · 0%
          - progressbar "上下文失败提示：0 次；0/1 · 0%"
      - region "路由决议完整数表":
        - table:
          - row "路由决议 次数 分支比例（分母为已知决议）":
            - columnheader "路由决议"
            - columnheader "次数"
            - columnheader "分支比例（分母为已知决议）"
          - row "快速回复 0 0/1 · 0%":
            - rowheader "快速回复"
            - cell "0"
            - cell "0/1 · 0%"
          - row "完整回答 0 0/1 · 0%":
            - rowheader "完整回答"
            - cell "0"
            - cell "0/1 · 0%"
          - row "图内保守兜底 0 0/1 · 0%":
            - rowheader "图内保守兜底"
            - cell "0"
            - cell "0/1 · 0%"
          - row "不回复 1 1/1 · 100%":
            - rowheader "不回复"
            - cell "1"
            - cell "1/1 · 100%"
          - row "上下文失败提示 0 0/1 · 0%":
            - rowheader "上下文失败提示"
            - cell "0"
            - cell "0/1 · 0%"
      - paragraph: 图后故障回退：发生 0 · 已知未发生 1 · 未知 0；发生率 0/1 · 0%；覆盖 1/1 · 100%
      - paragraph: 上下文恢复：发生 1 · 已知未发生 0 · 未知 0；发生率 1/1 · 100%；覆盖 1/1 · 100%
      - paragraph: 图前绕行：0 · 未知 0
      - paragraph: 被阻止的回退：0
  - region "手动聚合":
    - heading "手动聚合" [level=2]
    - paragraph: 单次动作 · 显式生成时固定目标会话与所选来源，草稿进入目标会话。草稿不自动进入工作窗口、后续聚合或提炼；再次生成会创建新运行。
    - text: 目标会话
    - combobox "目标会话":
      - option "请选择目标会话"
      - option "不用回复" [selected]
    - text: 聚合目标
    - textbox "聚合目标"
    - text: 聚合关键词
    - textbox "聚合关键词"
    - group "本次资料来源":
      - text: 本次资料来源
      - checkbox "语义记忆" [checked]
      - text: 语义记忆
      - checkbox "情景记忆" [checked]
      - text: 情景记忆
      - checkbox "会话窗口" [checked]
      - text: 会话窗口
    - paragraph: 未选择的来源不会读取；全不选来源仍可提交，将记录无动作。关键词仅用于所选记忆来源。
    - button "生成聚合草稿"
    - button "刷新会话"
    - paragraph: 表单没有新增的未提交修改。
    - status: 本次读取 1 个会话。刷新列表不会改变已提交目标。
    - status: 尚未提交聚合请求。
    - region "流程拓扑":
      - button "查看流程"
      - status: 服务已连接；结构仅在手动读取时更新。
      - status: 尚未读取；展开后读取。
- region "MainBar":
  - heading "主对话" [level=2]
  - text: 已同步
  - button "收起主对话"
  - text: 会话
  - button "新建会话"
  - region "主对话":
    - article:
      - paragraph: 不用回复
      - text: 已结束 · 按要求未回复 已保存
      - link "查看运行":
        - /url: /runs/3eadcb0dbeaf4a96acebfff04e0dc584
  - text: 消息
  - textbox "消息":
    - /placeholder: 写下消息；/skills 指定流程…
  - button "发送" [disabled]
  - paragraph: Enter 发送 · Shift+Enter 换行
- status: 就绪
- alert
```

# Test source

```ts
  1   | import {test, expect} from '@playwright/test';
  2   | import {memoryServer} from './memory-server.js';
  3   |
  4   | test('S06 CE-16/26: ratio bars use the same real sample and failures remain visible when collapsed', async ({page},testInfo) => {
  5   |   const server = await memoryServer({script:'tests/browser/statistics_fixed_server.py'});
  6   |   try {
  7   |     await page.goto(server.origin + '/behaviour');
  8   |     const panel = page.locator('.routing-statistics');
  9   |     await expect(panel.getByText('未读取；展开后读取统计。',{exact:true})).toBeVisible();
  10  |     await panel.getByText('路由统计',{exact:true}).click();
  11  |     await page.getByLabel('统计范围').selectOption('all');
  12  |     const full = panel.getByRole('progressbar',{name:'完整回答：3 次；3/6 · 50%',exact:true});
  13  |     await expect(full).toHaveAttribute('value','0.5');
  14  |     await expect(panel.locator('tr').filter({hasText:'完整回答'})).toContainText('3/6 · 50%');
  15  |     await page.setViewportSize({width:390,height:844});await full.scrollIntoViewIfNeeded();await page.screenshot({path:testInfo.outputPath('statistics-real-counts.png')});
  16  |     await page.route('**/api/behaviour/routing-statistics*',async route=>{
  17  |       const response = await route.fetch(), body = await response.json();
  18  |       delete body.groups.at(-1).decisions.ratios.full;await route.fulfill({response,json:body});
  19  |     });
  20  |     await panel.getByRole('button',{name:'刷新统计',exact:true}).click();
  21  |     await expect(panel.getByText(/统计响应不兼容/)).toBeVisible();
  22  |     await expect(panel.getByRole('progressbar')).toHaveCount(0);await expect(panel.locator('tr')).toHaveCount(0);
  23  |     await page.unroute('**/api/behaviour/routing-statistics*');
  24  |     await page.route('**/api/behaviour/routing-statistics*',route=>route.abort());
  25  |     await panel.getByRole('button',{name:'刷新统计',exact:true}).click();
  26  |     await expect(panel.getByText(/读取失败/)).toBeVisible();
  27  |     await panel.getByText('路由统计',{exact:true}).click();
  28  |     await expect(panel.getByText(/读取失败/)).toBeVisible();
  29  |     await expect(panel.getByRole('progressbar')).toHaveCount(0);
  30  |     await page.screenshot({path:testInfo.outputPath('statistics-collapsed-failure.png')});
  31  |   } finally {await server.close();}
  32  | });
  33  |
  34  | test('CE-15 statistics is collapsed, independent of unsaved settings, and keyboard readable', async ({page}) => {
  35  |   const server = await memoryServer({script:'tests/browser/routing_server.py'});
  36  |   try {
  37  |     await page.goto(server.origin + '/behaviour');
  38  |     const toggle = page.locator('.routing-statistics > summary');
  39  |     await expect(toggle).toBeVisible();
  40  |     await expect(page.getByRole('button', {name:'刷新统计', exact:true})).toBeHidden();
  41  |     await page.getByRole('checkbox', {name:'启用消息分流'}).check();
  42  |     await page.getByLabel('聚合目标', {exact:true}).fill('保留未提交的目标');
  43  |     await toggle.focus(); await page.keyboard.press('Enter');
  44  |     await expect(page.getByText('此范围内没有已准入的聊天 Run。')).toBeVisible();
  45  |     await expect(page.getByRole('checkbox', {name:'启用消息分流'})).toBeChecked();
  46  |     await expect(page.getByLabel('统计范围')).toHaveValue('7d');
  47  |     await expect(page.getByLabel('聚合目标', {exact:true})).toHaveValue('保留未提交的目标');
  48  |     await page.setViewportSize({width:390, height:844});
  49  |     await page.getByLabel('统计范围').selectOption('all');
  50  |     await expect(page.getByText(/范围：全部/)).toBeVisible();
  51  |     await page.screenshot({path:'tmp/agent-work/issue-76/narrow.png', fullPage:true});
  52  |     await page.reload();
  53  |     await expect(page.getByRole('button', {name:'刷新统计', exact:true})).toBeHidden();
  54  |   } finally {await server.close();}
  55  | });
  56  |
  57  | test('CE-03/08/14 real recovery counts survive restart and late range responses', async ({page}) => {
  58  |   const server = await memoryServer({script:'tests/browser/routing_server.py'});
  59  |   try {
  60  |     await page.goto(server.origin + '/behaviour');
  61  |     await page.getByRole('checkbox', {name:'启用消息分流'}).check();
  62  |     await page.getByRole('button', {name:'保存设置', exact:true}).click();
  63  |     await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
  64  |     await server.send('corrupt-context');
  65  |     // Use the real MainBar because it also owns and supplies session identity.
  66  |     await page.getByRole('button', {name:'新建会话', exact:true}).click();
  67  |     await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  68  |     await page.getByRole('textbox', {name:'消息'}).fill('不用回复');
  69  |     await page.getByRole('button', {name:'发送', exact:true}).click();
  70  |     await expect(page.locator('#messages').getByText('已保存', {exact:true})).toBeVisible();
  71  |     await page.getByText('路由统计', {exact:true}).click();
  72  |     const panel = page.locator('.routing-statistics');
  73  |     await expect(panel.getByText(/上下文恢复：发生 1/)).toBeVisible();
  74  |     await expect(panel.locator('tr').filter({hasText:'不回复'})).toContainText('1/1');
  75  |     await server.restart(); await page.reload();
  76  |     await page.getByText('路由统计', {exact:true}).click();
  77  |     await expect(panel.getByText(/上下文恢复：发生 1/)).toBeVisible();
  78  |     let release, observed;
  79  |     const gate = new Promise(resolve => release = resolve);
  80  |     const started = new Promise(resolve => observed = resolve);
  81  |     await page.route('**/api/behaviour/routing-statistics?window=7d', async route => {
  82  |       const response = await route.fetch(); observed(); await gate;
  83  |       await route.fulfill({response}).catch(() => {});
  84  |     });
  85  |     await page.getByRole('button', {name:'刷新统计', exact:true}).click();
  86  |     await started;
  87  |     await page.getByLabel('统计范围').selectOption('24h');
> 88  |     await expect(panel.getByText(/范围：24小时/)).toBeVisible();
      |                                              ^ Error: expect(locator).toBeVisible() failed
  89  |     release();
  90  |     await page.unroute('**/api/behaviour/routing-statistics?window=7d');
  91  |     await expect(panel.getByText(/范围：24小时/)).toBeVisible();
  92  |     await page.route('**/api/behaviour/routing-statistics*', route => route.abort());
  93  |     await page.getByLabel('统计范围').selectOption('all');
  94  |     await expect(panel.getByText(/读取失败/)).toBeVisible();
  95  |     await expect(panel.getByText(/范围：24小时/)).toHaveCount(0);
  96  |     await page.unroute('**/api/behaviour/routing-statistics*');
  97  |     await page.getByRole('button', {name:'刷新统计', exact:true}).click();
  98  |     await expect(panel.getByText(/上下文恢复：发生 1/)).toBeVisible();
  99  |   } finally {await server.close();}
  100 | });
  101 |
  102 | test('CE-14/15 closed and departed panels cannot be revived by late data; no polling', async ({page}) => {
  103 |   const server = await memoryServer({script:'tests/browser/routing_server.py'});
  104 |   let release;
  105 |   try {
  106 |     await page.goto(server.origin + '/behaviour');
  107 |     let calls = 0;
  108 |     page.on('request', req => {if (req.url().includes('/api/behaviour/routing-statistics')) calls++;});
  109 |     await page.getByText('路由统计', {exact:true}).click();
  110 |     await expect(page.getByText('此范围内没有已准入的聊天 Run。')).toBeVisible();
  111 |     await page.clock.install(); await page.clock.fastForward(60_000);
  112 |     expect(calls).toBe(1);
  113 |     let ready, finished;
  114 |     const started = new Promise(resolve => ready = resolve);
  115 |     const gate = new Promise(resolve => release = resolve);
  116 |     const settled = new Promise(resolve => finished = resolve);
  117 |     await page.route('**/api/behaviour/routing-statistics*', async route => {
  118 |       const response = await route.fetch(); ready(); await gate;
  119 |       await route.fulfill({response}).catch(() => {}); finished();
  120 |     });
  121 |     await page.getByRole('button', {name:'刷新统计', exact:true}).click();
  122 |     await started;
  123 |     await page.getByText('路由统计', {exact:true}).click();
  124 |     release(); await settled;
  125 |     await expect(page.getByRole('button', {name:'刷新统计', exact:true})).toBeHidden();
  126 |     await expect(page.getByText('此范围内没有已准入的聊天 Run。')).toHaveCount(0);
  127 |     await page.unroute('**/api/behaviour/routing-statistics*');
  128 |     await page.goto(server.origin + '/runs');
  129 |     await expect(page.locator('.routing-statistics')).toHaveCount(0);
  130 |     await page.goto(server.origin + '/behaviour');
  131 |     await expect(page.getByRole('button', {name:'刷新统计', exact:true})).toBeHidden();
  132 |   } finally {release?.(); await server.close();}
  133 | });
  134 |
  135 | test('CE-01 full failure and graph-internal fallback have separate live HTTP and page denominators', async ({page}) => {
  136 |   const server = await memoryServer({script:'tests/browser/routing_server.py'});
  137 |   try {
  138 |     await page.goto(server.origin + '/behaviour');
  139 |     await page.getByRole('checkbox', {name:'启用消息分流'}).check();
  140 |     await page.getByRole('button', {name:'保存设置', exact:true}).click();
  141 |     await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
  142 |     await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  143 |     for (const task of ['STATS graph fallback', 'STATS full failure', 'STATS classifier failure']) {
  144 |       const previousSession = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
  145 |       await page.getByRole('button', {name:'新建会话', exact:true}).click();
  146 |       await expect.poll(() => page.evaluate(() => sessionStorage.getItem('alfred.session'))).not.toBe(previousSession);
  147 |       await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
  148 |       await page.getByRole('textbox', {name:'消息'}).fill(task);
  149 |       const admitted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
  150 |       await page.getByRole('button', {name:'发送', exact:true}).click();
  151 |       const response = await admitted, identity = await response.json();
  152 |       expect(response.status(), JSON.stringify(identity)).toBe(202);
  153 |       await expect.poll(async () => {
  154 |         const runs = await (await page.request.get(server.origin + '/api/runs?filter=chat&limit=25')).json();
  155 |         return runs.runs.find(r => r.run_id === identity.run_id)?.phase;
  156 |       }).toBe('finished');
  157 |       await expect(page.locator('#messages').getByText('已保存', {exact:true})).toBeVisible();
  158 |     }
  159 |     await page.getByText('路由统计', {exact:true}).click();
  160 |     const panel = page.locator('.routing-statistics');
  161 |     await expect(panel.locator('tr').filter({hasText:'图内保守兜底'})).toContainText('1/2');
  162 |     await expect(panel.locator('tr').filter({hasText:'完整回答'})).toContainText('1/2');
  163 |     await expect(panel.getByText(/图后故障回退：发生 2/)).toContainText('2/3');
  164 |     await expect(panel.getByText(/^终态 3/)).toContainText('明确未形成决议 1');
  165 |     await page.screenshot({path:'tmp/agent-work/issue-76/statistics.png', fullPage:true});
  166 |   } finally {await server.close();}
  167 | });
  168 |
  169 | test('CE-15 statistics preserves a real aggregation request awaiting its receipt', async ({page}) => {
  170 |   const server = await memoryServer({script:'tests/browser/routing_server.py'});
  171 |   let release;
  172 |   try {
  173 |     await page.goto(server.origin + '/behaviour');
  174 |     await page.getByRole('button', {name:'新建会话', exact:true}).click();
  175 |     await expect.poll(() => page.evaluate(() => sessionStorage.getItem('alfred.session'))).not.toBeNull();
  176 |     const session = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
  177 |     await page.getByRole('button', {name:'刷新会话', exact:true}).click();
  178 |     await page.getByLabel('目标会话', {exact:true}).selectOption(session);
  179 |     await page.getByLabel('聚合目标', {exact:true}).fill('保持这份草稿请求');
  180 |     for (const name of ['语义记忆','情景记忆','会话窗口']) await page.getByRole('checkbox', {name, exact:true}).uncheck();
  181 |     let ready, completed;
  182 |     const started = new Promise(resolve => ready = resolve);
  183 |     const gate = new Promise(resolve => release = resolve);
  184 |     const settled = new Promise(resolve => completed = resolve);
  185 |     await page.route('**/api/runs', async route => {
  186 |       if (route.request().method() !== 'POST') return route.continue();
  187 |       const response = await route.fetch(); expect(response.status()).toBe(202);
  188 |       ready(); await gate; await route.fulfill({response}); completed();
```
