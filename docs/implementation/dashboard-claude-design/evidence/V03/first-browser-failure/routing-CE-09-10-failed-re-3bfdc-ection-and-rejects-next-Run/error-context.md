# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: routing.spec.js >> CE-09/10: failed recording keeps no-reply projection and rejects next Run
- Location: tests/browser/routing.spec.js:88:1

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: locator('#messages').getByText('已结束 · 按要求未回复')
Expected: visible
Timeout: 4000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" locator('#messages').getByText('已结束 · 按要求未回复') with timeout 4000ms
  - waiting for locator('#messages').getByText('已结束 · 按要求未回复')

```

```yaml
- banner "导航面板":
  - link "Alfred 本地助手":
    - /url: /overview
  - paragraph: 记录服务不可用
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
    - paragraph: 保存失败 · Web · 记录服务不可用 · 未保存
    - link "查看当前运行":
      - /url: /runs/32beae518125458ebd067508f3057029?filter=chat
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
    - status: 已保存；下一 Run 生效。
    - region "流程拓扑":
      - button "查看流程"
      - status: 服务已连接；结构仅在手动读取时更新。
      - status: 尚未读取；展开后读取。
    - group:
      - text: 路由统计
      - status: 未读取；展开后读取统计。
  - region "手动聚合":
    - heading "手动聚合" [level=2]
    - paragraph: 单次动作 · 显式生成时固定目标会话与所选来源，草稿进入目标会话。草稿不自动进入工作窗口、后续聚合或提炼；再次生成会创建新运行。
    - text: 目标会话
    - combobox "目标会话":
      - option "请选择目标会话" [selected]
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
    - button "生成聚合草稿" [disabled]
    - button "刷新会话"
    - paragraph: 表单没有新增的未提交修改。
    - status: 本次读取 0 个会话。刷新列表不会改变已提交目标。
    - paragraph: 记录服务不可用；先处理未保存状态。
    - status: 尚未提交聚合请求。
    - region "流程拓扑":
      - button "查看流程"
      - status: 服务已连接；结构仅在手动读取时更新。
      - status: 尚未读取；展开后读取。
- region "MainBar":
  - heading "主对话" [level=2]
  - text: 记录不可用
  - button "收起主对话"
  - region "当前运行":
    - strong: 保存失败
    - paragraph: 不用回复
    - text: Web · 2026-10-12T06:07:07Z · Step 1
    - link "查看当前运行":
      - /url: http://127.0.0.1:18034/runs/32beae518125458ebd067508f3057029?filter=chat
  - text: 会话
  - button "新建会话" [disabled]
  - region "主对话":
    - article:
      - paragraph: 不用回复
      - text: 已结束 · 按要求未回复 本次运行未保存
      - link "查看运行":
        - /url: /runs/32beae518125458ebd067508f3057029
  - status: 记录服务不可用；草稿已保留。
  - text: 消息
  - textbox "消息":
    - /placeholder: 写下消息；/skills 指定流程…
  - button "发送" [disabled]
  - paragraph: 记录服务不可用；不会排队发送。
- status: 运行中
- alert: 本次运行未保存
```

# Test source

```ts
  1   | import {observeTopology} from './topology-observation.js';
  2   | import {test, expect} from '@playwright/test';
  3   | import {memoryServer, api} from './memory-server.js';
  4   |
  5   | test('S06 CE-13: read-only comparison cannot rebase a dirty routing choice', async ({page}) => {
  6   |   const server = await memoryServer({script:'tests/browser/routing_server.py'});
  7   |   try {
  8   |     await page.goto(server.origin + '/behaviour');
  9   |     const choice = page.getByRole('checkbox', {name:'启用消息分流'});
  10  |     await expect(choice).toBeEnabled();
  11  |     const saved = await (await page.request.get(server.origin + '/api/behaviour')).json();
  12  |     await choice.check();
  13  |     const {csrf_token} = await (await page.request.get(server.origin + '/api/entry')).json();
  14  |     const external = await page.request.post(server.origin + '/api/behaviour', {
  15  |       headers:{'x-agent-alfred-csrf':csrf_token},
  16  |       data:{action:'save',enabled:true,expected_revision:saved.revision},
  17  |     });
  18  |     expect(external.status()).toBe(200);
  19  |     const refreshed = page.waitForResponse(r => r.url().endsWith('/api/behaviour') && r.request().method()==='GET');
  20  |     await page.getByRole('button',{name:'刷新设置',exact:true}).click();
  21  |     await refreshed;
  22  |     await observeTopology(page);
  23  |     await choice.uncheck(); // Still an edit of the original version, not consent to the external revision.
  24  |     await choice.check();
  25  |     const attempted = page.waitForResponse(r => r.url().endsWith('/api/behaviour') && r.request().method()==='POST');
  26  |     await page.getByRole('button',{name:'保存设置',exact:true}).click();
  27  |     const response = await attempted;
  28  |     expect(response.request().postDataJSON().expected_revision).toBe(saved.revision);
  29  |     expect(response.status()).toBe(409);
  30  |     await expect(page.getByText(/settings_conflict/)).toBeVisible();
  31  |     await expect(choice).toBeChecked();
  32  |     await expect(page.getByRole('region',{name:'消息分流',exact:true}).locator('.behaviour-draft')).toHaveAttribute('data-dirty','true');
  33  |     await page.getByRole('link',{name:'运行',exact:true}).click();
  34  |     await expect(page.getByRole('dialog')).toBeVisible();
  35  |     await page.getByRole('button',{name:'留在此页',exact:true}).click();
  36  |     await page.getByRole('button',{name:'基于当前版本继续编辑',exact:true}).click();
  37  |     await choice.uncheck();
  38  |     await page.getByRole('button',{name:'保存设置',exact:true}).click();
  39  |     await expect(page.getByText('已保存；下一 Run 生效。',{exact:true})).toBeVisible();
  40  |     expect((await (await page.request.get(server.origin+'/api/behaviour')).json()).enabled).toBe(false);
  41  |   } finally {await server.close();}
  42  | });
  43  |
  44  | test('CE-07/09/12: Behaviour, no reply, refresh and real restart', async ({page}) => {
  45  |   const server = await memoryServer({script: 'tests/browser/routing_server.py'});
  46  |   try {
  47  |     await page.goto(server.origin + '/behaviour');
  48  |     await page.getByRole('checkbox', {name: '启用消息分流'}).check();
  49  |     await page.getByRole('button', {name: '保存设置', exact: true}).click();
  50  |     await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
  51  |     await page.getByRole('button', {name: '新建会话', exact: true}).click();
  52  |     await expect(page.getByRole('textbox', {name: '消息'})).toBeEnabled();
  53  |     const session = await page.evaluate(() => sessionStorage.getItem('alfred.session'));
  54  |     await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  55  |     await page.getByRole('textbox', {name: '消息'}).fill('不用回复');
  56  |     const accepted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
  57  |     await page.getByRole('button', {name: '发送', exact: true}).click();
  58  |     const admission = await accepted, identity = await admission.json();
  59  |     expect(admission.status(), JSON.stringify(identity)).toBe(202);
  60  |     expect(identity).toMatchObject({run_id: expect.any(String), session_id: session});
  61  |     const messages = page.locator('#messages');
  62  |     await expect(messages.getByText('已结束 · 按要求未回复')).toBeVisible();
  63  |     await expect(messages.getByText('运行已结束', {exact:true})).toHaveCount(0);
  64  |     // A no-reply terminal projection is visible before recording commits.
  65  |     await expect(messages.getByText('已保存', {exact:true})).toBeVisible();
  66  |     const other = await api(page.request, server.origin);
  67  |     const runs = await other.get('/api/runs?filter=chat&limit=25');
  68  |     expect(runs.status, JSON.stringify(runs.body)).toBe(200);
  69  |     const recorded = runs.body.runs.find(r => r.run_id === identity.run_id);
  70  |     expect(recorded).toMatchObject({session_id: session, prompt_preview: '不用回复', phase: 'finished', outcome: 'completed'});
  71  |     const run = recorded.run_id;
  72  |     await page.goto(`${server.origin}/runs/${run}`);
  73  |     await expect(page.getByText('user_requested_no_reply', {exact:false}).first()).toBeVisible();
  74  |     await server.restart();
  75  |     await page.reload();
  76  |     await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toBeVisible();
  77  |     const entry = await (await page.request.get(server.origin + '/api/entry')).json();
  78  |     const response = await page.request.get(server.origin + '/api/reply?' + new URLSearchParams({
  79  |       process_instance_id:entry.instance_id, session_id:session, run_id:run,
  80  |     }));
  81  |     expect(response.status()).toBe(200);
  82  |     expect((await response.json()).reply_disposition).toBe('no_reply');
  83  |     await page.goto(server.origin + '/behaviour');
  84  |     await expect(page.getByRole('checkbox', {name:'启用消息分流'})).toBeChecked();
  85  |   } finally {await server.close();}
  86  | });
  87  |
  88  | test('CE-09/10: failed recording keeps no-reply projection and rejects next Run', async ({page}) => {
  89  |   const server = await memoryServer({script: 'tests/browser/routing_server.py'});
  90  |   try {
  91  |     await page.goto(server.origin + '/behaviour');
  92  |     await page.getByRole('checkbox', {name:'启用消息分流'}).check();
  93  |     await page.getByRole('button', {name:'保存设置', exact:true}).click();
  94  |     await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
  95  |     await page.getByRole('button', {name:'新建会话', exact:true}).click();
  96  |     await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
  97  |     await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  98  |     await server.send('fail-recording');
  99  |     await page.getByRole('textbox', {name:'消息'}).fill('不用回复');
  100 |     await page.getByRole('button', {name:'发送', exact:true}).click();
> 101 |     await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toBeVisible();
      |                                                                       ^ Error: expect(locator).toBeVisible() failed
  102 |     await expect(page.locator('#messages').getByText('本次运行未保存', {exact:true})).toBeVisible();
  103 |     await page.reload();
  104 |     await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toBeVisible();
  105 |     await expect(page.locator('#messages').getByText('正文未完整加载')).toHaveCount(0);
  106 |     await expect(page.getByRole('button', {name:'发送', exact:true})).toBeDisabled();
  107 |     const href = await page.getByRole('region', {name:'当前运行', exact:true}).getByRole('link', {name:'查看当前运行'}).getAttribute('href');
  108 |     const run = {run_id:new URL(href, server.origin).pathname.split('/').pop(),
  109 |       session_id:await page.evaluate(() => sessionStorage.getItem('alfred.session'))};
  110 |     await server.send('repair-recording');
  111 |     await server.restart();
  112 |     await page.goto(`${server.origin}/runs/${run.run_id}`);
  113 |     await expect(page.getByText(/运行终态无法确认|执行前中断/).first()).toBeVisible();
  114 |     const entry = await (await page.request.get(server.origin + '/api/entry')).json();
  115 |     const response = await page.request.get(server.origin + '/api/reply?' + new URLSearchParams({
  116 |       process_instance_id:entry.instance_id, session_id:run.session_id, run_id:run.run_id,
  117 |     }));
  118 |     expect(response.status()).toBe(503);
  119 |     expect((await response.json()).code).toBe('reply_unavailable');
  120 |     await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toHaveCount(0);
  121 |   } finally {await server.close();}
  122 | });
  123 |
  124 | test('CE-09: recovery facts and fixed text survive trace removal and restart', async ({page}) => {
  125 |   const server = await memoryServer({script:'tests/browser/routing_server.py'});
  126 |   try {
  127 |     await page.goto(server.origin + '/behaviour');
  128 |     await page.getByRole('checkbox', {name:'启用消息分流'}).check();
  129 |     await page.getByRole('button', {name:'保存设置', exact:true}).click();
  130 |     await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
  131 |     await server.send('corrupt-context');
  132 |     await page.getByRole('button', {name:'新建会话', exact:true}).click();
  133 |     await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
  134 |     await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  135 |     for (const task of ['不用回复', '解释上下文']) {
  136 |       await page.getByRole('textbox', {name:'消息'}).fill(task);
  137 |       await page.getByRole('button', {name:'发送', exact:true}).click();
  138 |       if (task === '不用回复')
  139 |         await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toBeVisible();
  140 |       else
  141 |         await expect(page.locator('#messages').getByText('本次上下文准备失败，请稍后重试。')).toBeVisible();
  142 |       await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
  143 |     }
  144 |     await server.send('trim-traces');
  145 |     await server.restart();
  146 |     await page.reload();
  147 |     await expect(page.locator('#messages').getByText('已结束 · 按要求未回复')).toBeVisible();
  148 |     await expect(page.locator('#messages').getByText('本次上下文准备失败，请稍后重试。')).toBeVisible();
  149 |     const other = await api(page.request, server.origin);
  150 |     const runs = await other.get('/api/runs?filter=chat&limit=25');
  151 |     for (const run of runs.body.runs) {
  152 |       await page.goto(`${server.origin}/runs/${run.run_id}`);
  153 |       await expect(page.getByText('project_context', {exact:false}).first()).toBeVisible();
  154 |     }
  155 |   } finally {await server.close();}
  156 | });
  157 |
  158 | test('CE-13: page fingerprint rejects external change and explicitly backs up recovery', async ({page}) => {
  159 |   const {writeFile, readFile, readdir} = await import('node:fs/promises');
  160 |   const {join} = await import('node:path');
  161 |   const server = await memoryServer({script:'tests/browser/routing_server.py',
  162 |     prepare: directory => writeFile(join(directory, 'behaviour.json'), 'broken A')});
  163 |   try {
  164 |     await page.goto(server.origin + '/behaviour');
  165 |     const recover = page.getByRole('button', {name:'备份原文件并恢复为关闭'});
  166 |     await expect(recover).toBeVisible();
  167 |     await writeFile(join(server.directory, 'behaviour.json'), 'broken B');
  168 |     await recover.click();
  169 |     await expect(page.getByText(/settings_conflict/)).toBeVisible();
  170 |     await observeTopology(page); // #75 CE-01: graph reads preserve the real conflict.
  171 |     await expect(page.getByText(/settings_conflict/)).toBeVisible();
  172 |     expect(await readFile(join(server.directory, 'behaviour.json'), 'utf8')).toBe('broken B');
  173 |     await page.getByRole('button', {name:'刷新设置'}).click();
  174 |     await expect(page.getByText(/配置不可用/)).toBeVisible();
  175 |     await recover.click();
  176 |     await expect(page.getByText('已备份原文件并恢复为关闭；下一 Run 生效。')).toBeVisible();
  177 |     const backup = (await readdir(server.directory)).find(p => p.startsWith('behaviour.json.backup-'));
  178 |     expect(await readFile(join(server.directory, backup), 'utf8')).toBe('broken B');
  179 |     await server.restart();
  180 |     await page.reload();
  181 |     await expect(page.getByRole('checkbox', {name:'启用消息分流'})).not.toBeChecked();
  182 |   } finally {await server.close();}
  183 | });
  184 |
  185 | test('CE-01: disabled real browser conversation matches exact pre-routing baseline', async ({browser}) => {
  186 |   const {spawn, execFileSync} = await import('node:child_process');
  187 |   const {mkdtemp, rm} = await import('node:fs/promises');
  188 |   const {tmpdir} = await import('node:os');
  189 |   const {join, resolve} = await import('node:path');
  190 |   const baseline = await mkdtemp(join(tmpdir(), 'routing-baseline-'));
  191 |   const root = process.cwd();
  192 |   const archive = execFileSync('git', ['archive', '8ba7192ee84533aef3deb8453d8fa7364d67f48b'], {maxBuffer:64 * 1024 * 1024});
  193 |   execFileSync(resolve('.venv/bin/python'), ['-c',
  194 |     'import io,sys,tarfile; tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())).extractall(sys.argv[1],filter="data")', baseline], {input:archive});
  195 |   const texts = [];
  196 |   try {
  197 |     for (const source of [baseline, root]) {
  198 |       const server = await memoryServer({script:'tests/browser/skills_server.py',
  199 |         spawnProcess: (_command, args) => spawn(resolve(root, '.venv/bin/python'), args, {cwd:source})});
  200 |       const context = await browser.newContext();
  201 |       try {
```
