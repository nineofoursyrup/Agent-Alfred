# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: database_status.spec.js >> S10 failed cleanup blocks queries until both release and capability are verified
- Location: tests/browser/database_status.spec.js:127:1

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: getByText('清理已释放', { exact: true })
Expected: visible
Timeout: 4000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" getByText('清理已释放', { exact: true }) with timeout 4000ms
  - waiting for getByText('清理已释放', { exact: true })

```

```yaml
- banner "导航面板":
  - link "Alfred 本地助手":
    - /url: /overview
  - paragraph: 已同步
  - paragraph: 127.0.0.1:18124
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
  - paragraph: DATABASE
  - heading "数据库" [level=1]
  - paragraph: 只读查询当前实例的诊断数据，结果遵守脱敏与遗忘边界。
  - button "打开主对话" [expanded]
  - region "运行与连接摘要":
    - paragraph: 尚未选择会话
  - paragraph: 查询受保护的临时数据。SQL 的 WHERE／LIMIT 不缩小准备范围。结果有上限，不是数据库总量。
  - region "诊断对象目录":
    - paragraph: 当前实例固定。记忆修订 11 · 保护规则版本 5
    - paragraph: 可用性：已核验可用
    - paragraph: 执行预算 5 秒 · 最多 1000 行／64 列／2 MiB JSON
    - group: 对象目录
    - group: 完整限制与寿命
  - text: SQL 草稿
  - textbox "SQL":
    - /placeholder: SELECT * FROM diag_sessions LIMIT 20
    - text: SELECT * FROM diag_sessions
  - paragraph: Enter 换行。仅点击执行才提交；查询中可编辑下一份草稿。
  - button "执行" [disabled]
  - button "取消" [disabled]
  - button "核验查询与可用性"
  - button "复制 SQL"
  - paragraph: 查询清理尚未确认释放；请核验查询与可用性。
  - paragraph: 记忆或保护版本变化：已清除结果及提交关联；草稿保留，核验后仍需显式执行。
  - region "查询与资源状态":
    - heading "执行" [level=3]
    - paragraph: 失效
    - heading "正文" [level=3]
    - paragraph: 正文已清除；晚到响应不会恢复。
    - heading "清理" [level=3]
    - paragraph: 清理失败
  - region "已提交查询"
  - button "复制本页结果" [disabled]
  - status: 尚无有效结果可复制。
  - region "查询结果"
- region "MainBar":
  - heading "主对话" [level=2]
  - button "收起主对话"
  - text: 尚未选择会话
  - button "新建会话"
  - region "主对话"
  - text: 消息
  - textbox "消息" [disabled]:
    - /placeholder: 写下消息；/skills 指定流程…
  - button "发送" [disabled]
  - paragraph: 请显式新建或继续会话。
- status: 就绪
- alert
```

# Test source

```ts
  52  |     await page.route('**/api/database/queries/*/execute', route => route.abort('failed'));
  53  |     await page.route('**/api/database/queries/*', async route => {
  54  |       if (route.request().method() !== 'GET') return route.fallback();
  55  |       await route.fulfill({json: {status: state, cleanup}});
  56  |     });
  57  |     for (const [value, expected] of [
  58  |       ['unused', '尚未执行'], ['prepared', '准备／执行中'], ['running', '准备／执行中'],
  59  |       ['stopping', '已请求取消'], ['completed', '结果未收到'],
  60  |       ['cancelled', cleanup === 'released' ? '已实际停止' : '已请求取消'],
  61  |       ['timed_out', '超时'], ['invalidated', '失效'], ['failed', '执行失败'],
  62  |     ]) {
  63  |       state = value;
  64  |       await page.goto('/database');
  65  |       await expect(page.getByText('可执行', {exact:true})).toBeVisible();
  66  |       const response = page.waitForResponse(r => r.request().method() === 'GET' && /\/api\/database\/queries\/[^/]+$/.test(new URL(r.url()).pathname));
  67  |       await page.getByRole('button', {name: '执行', exact: true}).click();
  68  |       await response;
  69  |       await expect(page.locator('#page [data-state]')).toHaveText(expected);
  70  |       await expect(page.getByText(cleanup === 'failed' ? '清理失败' : cleanup === 'pending' ? '清理中' : '清理已释放', {exact:true})).toBeVisible();
  71  |       if (cleanup !== 'released') await expect(page.getByRole('button',{name:'执行',exact:true})).toBeDisabled();
  72  |       await expect(page.getByRole('region', {name: '查询结果'})).toBeEmpty();
  73  |     }
  74  |   });
  75  | }
  76  |
  77  | for (const [code, http, expected] of [
  78  |   ['handle_expired', 410, '不可用 · 句柄已过期'],
  79  |   ['not_found', 404, '不可用 · 没有这条查询记录'],
  80  |   ['instance_changed', 409, '不可用 · 实例已更换'],
  81  | ]) {
  82  |   test(`status lookup error ${code} is not a cleanup failure`, async ({page}) => {
  83  |     await page.goto('/database');
  84  |     await expect(page.getByText('可执行', {exact: true})).toBeVisible();
  85  |     await page.route('**/api/database/queries/*/execute', route => route.abort('failed'));
  86  |     await page.route('**/api/database/queries/*', async route => {
  87  |       if (route.request().method() !== 'GET') return route.fallback();
  88  |       await route.fulfill({status: http, json: {code}});
  89  |     });
  90  |     await page.getByRole('button', {name: '执行', exact: true}).click();
  91  |     await expect(page.locator('#page [data-state]')).toHaveText(expected);
  92  |   });
  93  | }
  94  |
  95  | test('STD-06/SPEC-12: actual worker stop failure is cleanup failure', async ({page}) => {
  96  |   const server = await memoryServer({script: 'tests/browser/database_server.py'});
  97  |   try {
  98  |     await page.goto(server.origin + '/database');
  99  |     await expect(page.getByText('可执行', {exact: true})).toBeVisible();
  100 |     await server.send('arm');
  101 |     await server.send('kill-fail');
  102 |     await page.getByRole('textbox', {name: 'SQL', exact: true}).fill('SELECT * FROM diag_sessions');
  103 |     await page.getByRole('button', {name: '执行', exact: true}).click();
  104 |     await server.send('running');
  105 |     const cancelled = page.waitForResponse(r => r.url().endsWith('/cancel'));
  106 |     await page.getByRole('button', {name: '取消', exact: true}).click();
  107 |     const response = await cancelled;
  108 |     const body = await response.json();
  109 |     expect(body.cleanup).toBe('failed');
  110 |     expect(body.status).toBe('failed');
  111 |     await expect(page.getByText('清理失败', {exact: true})).toBeVisible();
  112 |     const status = await (await page.request.get(`${server.origin}/api/database/queries/${body.query_id}`)).json();
  113 |     expect(status.cleanup).toBe('failed');
  114 |     const entry = await (await page.request.get(server.origin + '/api/entry')).json();
  115 |     const denied = await page.request.post(server.origin + '/api/database/queries', {
  116 |       headers: {'x-agent-alfred-csrf': entry.csrf_token}, data: {},
  117 |     });
  118 |     expect(denied.status()).toBe(503);
  119 |     expect((await denied.json()).code).toBe('cleanup_failed');
  120 |     console.log('actual-cleanup-failure', JSON.stringify({cancel: body, status, newHandleStatus: denied.status()}));
  121 |     await server.send('heal');
  122 |     const recovered = await (await page.request.get(`${server.origin}/api/database/queries/${body.query_id}`)).json();
  123 |     expect(recovered.cleanup).toBe('released');
  124 |   } finally {await server.close();}
  125 | });
  126 |
  127 | test('S10 failed cleanup blocks queries until both release and capability are verified', async ({page}) => {
  128 |   const server = await memoryServer({script: 'tests/browser/database_server.py'});
  129 |   try {
  130 |     await page.goto(server.origin + '/database');
  131 |     await expect(page.getByText('可执行', {exact:true})).toBeVisible();
  132 |     await server.send('arm');
  133 |     await server.send('kill-fail');
  134 |     await page.getByRole('textbox', {name:'SQL',exact:true}).fill('SELECT * FROM diag_sessions');
  135 |     await page.getByRole('button', {name:'执行',exact:true}).click();
  136 |     await server.send('running');
  137 |     const cancelled = page.waitForResponse(r => r.url().endsWith('/cancel'));
  138 |     await page.getByRole('button', {name:'取消',exact:true}).click();
  139 |     const receipt = await (await cancelled).json();
  140 |     expect(receipt.cleanup).toBe('failed');
  141 |     await expect(page.getByText('清理失败',{exact:true})).toBeVisible();
  142 |     const run = page.getByRole('button', {name:'执行',exact:true});
  143 |     await expect(run).toBeDisabled();
  144 |     let newHandles = 0;
  145 |     page.on('request', r => {if(r.url().endsWith('/api/database/queries')) newHandles++;});
  146 |     await run.evaluate(el => el.click());
  147 |     expect(newHandles).toBe(0);
  148 |     await page.getByRole('button', {name:'核验查询与可用性',exact:true}).click();
  149 |     await expect(run).toBeDisabled();
  150 |     await server.send('heal');
  151 |     await page.getByRole('button', {name:'核验查询与可用性',exact:true}).click();
> 152 |     await expect(page.getByText('清理已释放',{exact:true})).toBeVisible();
      |                                                        ^ Error: expect(locator).toBeVisible() failed
  153 |     await expect(run).toBeEnabled();
  154 |     await page.getByRole('textbox', {name:'SQL',exact:true}).fill('SELECT 7 AS recovered');
  155 |     await run.click();
  156 |     await expect(page.getByRole('region', {name:'查询结果'}).locator('td')).toHaveText('7');
  157 |   } finally {await server.close();}
  158 | });
  159 |
  160 | for (const lost of ['issuance', 'execute']) {
  161 |   test(`S10 lost ${lost} receipt preserves the boundary for explicit capability recovery`, async ({page}, testInfo) => {
  162 |     const server = await memoryServer();
  163 |     const evidence = {lost, handles:[], executes:0, lostStatusReads:0};
  164 |     try {
  165 |       await page.goto(server.origin+'/database');
  166 |       await expect(page.getByText('可执行',{exact:true})).toBeVisible();
  167 |       const sql = page.getByRole('textbox',{name:'SQL',exact:true});
  168 |       const run = page.getByRole('button',{name:'执行',exact:true});
  169 |       const recheck = page.getByRole('button',{name:'核验查询与可用性',exact:true});
  170 |       page.on('request', request => {if(request.url().endsWith('/execute')) evidence.executes++;});
  171 |       await page.route('**/api/database/queries', async route => {
  172 |         const response = await route.fetch();
  173 |         expect(response.status()).toBe(200);
  174 |         evidence.handles.push((await response.json()).query_id);
  175 |         if (lost === 'issuance' && evidence.handles.length === 1) await route.abort('failed');
  176 |         else await route.fulfill({response});
  177 |       });
  178 |       if (lost === 'execute') {
  179 |         await page.route('**/api/database/queries/*/execute', async route => {
  180 |           const response = await route.fetch();
  181 |           expect(response.status()).toBe(200);
  182 |           await route.abort('failed');
  183 |         }, {times:1});
  184 |         await page.route('**/api/database/queries/*', async route => {
  185 |           if(route.request().method() !== 'GET') return route.fallback();
  186 |           evidence.lostStatusReads++;
  187 |           await route.abort('failed');
  188 |         });
  189 |       }
  190 |       await sql.fill('SELECT 42 AS first_draft');
  191 |       await run.click();
  192 |       await expect(page.locator('[data-state]')).toHaveAttribute('data-state','unavailable');
  193 |       await expect(run).toBeDisabled();
  194 |       await expect(sql).toHaveValue('SELECT 42 AS first_draft');
  195 |       expect(evidence.handles).toHaveLength(1);
  196 |       expect(evidence.executes).toBe(lost === 'issuance' ? 0 : 1);
  197 |       // Independent public read observes the real handle; the browser has not
  198 |       // received this identity/status and must not use test knowledge as proof.
  199 |       evidence.actual = await (await page.request.get(server.origin+'/api/database/queries/'+evidence.handles[0])).json();
  200 |       expect(evidence.actual.status).toBe(lost === 'issuance' ? 'unused' : 'completed');
  201 |       expect(evidence.actual.cleanup).toBe('released');
  202 |       const catalog = await (await page.request.get(server.origin+'/api/database')).json();
  203 |       evidence.capability = {instance:catalog.instance_id, available:catalog.available};
  204 |       expect(catalog.available).toBe(true);
  205 |       expect(catalog.instance_id).toBe(evidence.actual.instance_id);
  206 |       if(lost === 'issuance') {
  207 |         const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/database');
  208 |         await recheck.click();
  209 |         expect((await (await response).json()).available).toBe(true);
  210 |         await expect(run).toBeEnabled();
  211 |         await expect(page.getByText('可执行',{exact:true})).toBeVisible();
  212 |       } else {
  213 |         const failed = page.waitForEvent('requestfailed', {predicate:r => r.method()==='GET' && r.url().endsWith('/'+evidence.handles[0])});
  214 |         await recheck.click();
  215 |         await failed;
  216 |         await expect(run).toBeDisabled();
  217 |         expect(evidence.lostStatusReads).toBe(2);
  218 |         expect(evidence.handles).toHaveLength(1);
  219 |         expect(evidence.executes).toBe(1);
  220 |         // A real known-handle status, not merely catalog availability, is
  221 |         // required after the browser has sent execution.
  222 |         await page.unroute('**/api/database/queries/*');
  223 |         await recheck.click();
  224 |         await expect(run).toBeEnabled();
  225 |         await expect(page.getByText('结果未收到',{exact:true})).toBeVisible();
  226 |       }
  227 |       await expect(page.getByRole('region',{name:'查询结果',exact:true})).toBeEmpty();
  228 |       expect(evidence.handles).toHaveLength(1);
  229 |       expect(evidence.executes).toBe(lost === 'issuance' ? 0 : 1);
  230 |       await sql.fill('SELECT 84 AS explicit_next');
  231 |       await run.click();
  232 |       await expect(page.getByRole('region',{name:'查询结果',exact:true}).locator('td')).toHaveText('84');
  233 |       expect(evidence.handles).toHaveLength(2);
  234 |       expect(evidence.executes).toBe(lost === 'issuance' ? 1 : 2);
  235 |       console.log('lost-receipt-recovery',JSON.stringify(evidence));
  236 |     } finally {
  237 |       try {await page.close();} finally {await server.close();}
  238 |       await testInfo.attach('actual-receipt-recovery',{body:JSON.stringify(evidence,null,2),contentType:'application/json'});
  239 |     }
  240 |   });
  241 | }
  242 |
```
