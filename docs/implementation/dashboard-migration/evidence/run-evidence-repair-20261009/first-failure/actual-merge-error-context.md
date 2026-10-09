# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: run-path.spec.js >> CE-11/AC-19 keyboard graph/text equivalence 1280
- Location: tests/browser/run-path.spec.js:57:73

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: getByRole('region', { name: '本次执行路径', exact: true }).locator('[aria-label="节点详情"] a').first()
Expected: visible
Timeout: 4000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" getByRole('region', { name: '本次执行路径', exact: true }).locator('[aria-label="节点详情"] a').first() with timeout 4000ms
  - waiting for getByRole('region', { name: '本次执行路径', exact: true }).locator('[aria-label="节点详情"] a').first()

```

```yaml
- banner "导航面板":
  - link "Agent-Alfred 本地助手":
    - /url: /overview
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
- button "打开主对话" [expanded]
- region "运行与连接摘要":
  - paragraph: 已同步
- main:
  - heading "运行" [level=1]
  - status
  - link "返回运行列表":
    - /url: /runs?filter=all
  - button "在主对话中查看"
  - link "查看追踪导出":
    - /url: "#trace-export"
  - region "运行摘要":
    - heading "运行摘要" [level=2]
    - article:
      - term: Run ID
      - definition: 75058e76a0ae4f848ccf2c371ff19ffd
      - term: 用途
      - definition: 普通聊天（chat）
      - term: 所属分组
      - definition: 会话运行
      - term: 来源
      - definition: web
      - term: 入口
      - definition: mainbar
      - term: 会话
      - definition: 4580e009736d41fabccf9f642043c387
      - term: 接受时间
      - definition: 2026-10-09T14:04:45Z
      - term: 开始时间
      - definition: 2026-10-09T14:04:45Z
      - term: 结束时间
      - definition: 2026-10-09T14:04:45Z
      - term: 准入
      - definition: 已获准
      - term: 阶段
      - definition: 已结束
      - term: 结果
      - definition: 回复完成
      - term: 记录
      - definition: 已保存
      - term: 记录来源
      - definition: durable_finalization
      - term: 输入预览
      - definition: 解释图执行
  - status: 运行摘要 · 读取于 2:04:45 PM
  - button "刷新运行摘要"
  - status
  - status: 过程证据共同来源 · 读取于 2:04:46 PM
  - button "刷新过程证据"
  - region "运行过程":
    - heading "过程证据" [level=2]
    - paragraph: 事件发布顺序
    - heading "Step 0" [level=3]
    - paragraph: 主结果 · committed
    - group: Attempt · seq 3 · committed
    - heading "Step 1" [level=3]
    - paragraph: 主结果 · committed
    - group: Attempt · seq 11 · committed
    - heading "Step 2" [level=3]
    - paragraph: 主结果 · committed
    - group: Attempt · seq 25 · committed
    - group: 本次输入
    - heading "消息分流" [level=3]
    - paragraph: full · classifier_full
    - paragraph: 图结果：Completed；代际 2
    - paragraph: 分类模型：opencode-go / deepseek-v4-flash
  - region "本次执行路径":
    - button "收起执行路径" [expanded]
    - status: 服务已连接；结构仅在手动读取时更新。
    - status: 读取时的执行路径 · 2026-10-09T14:04:45Z。来源：持久 trace；路径 available；Graph Completed；Run completed；记录 recorded。
    - button "重新读取"
    - button "放大"
    - button "缩小"
    - button "适应全图"
    - button "向左平移"
    - button "向右平移"
    - button "向上平移"
    - button "向下平移"
    - paragraph: 图例：实线＝步骤依赖；虚线＝条件分支；点线＝声明的恢复路径（不保证任意异常均可恢复）。出口以文字标明。
    - img "流程节点与连接图": quick full fallback no_action context_failure 判断消息类型 classify · 计算成功 · 已提交 整理已准备的上下文 project_context · 计算成功 · 已提交 标记上下文不可用 recover_context · 跳过 · 已提交 · all_inbound_not_taken 决定处理分支 join_route · 计算成功 · 已提交 完整处理 full_agent · 计算成功 · 已提交 返回完整处理结果 full_result · 计算成功 · 已提交 返回固定短回复 quick_result · 跳过 · 已提交 · all_inbound_not_taken 返回上下文失败提示 context_failure_result · 跳过 · 已提交 · all_inbound_not_taken 按要求不回复 no_reply_terminal · 跳过 · 已提交 · all_inbound_not_taken
    - heading "判断消息类型 · classify" [level=4]
    - paragraph: 仅分类当前任务，分类是控制信息；它不生成用户回复
    - paragraph: 节点类型：llm · 步骤 · 计算成功 · 已提交
    - group: 节点原始定义
    - paragraph: Step 1 · 关联证据不可用
    - paragraph: Attempt 07749ce665d940cb9aa1661fc6ba7c45 · 关联证据不可用
    - heading "完整文字明细" [level=3]
    - paragraph: 仅展示读取边界内可证明的事实。taken 不证明目标成功；波撤销不撤回工具副作用或费用。
    - list:
      - listitem:
        - button "判断消息类型 · classify"
        - paragraph: 仅分类当前任务，分类是控制信息；它不生成用户回复
        - paragraph: 节点类型：llm · 步骤 · 计算成功 · 已提交
      - listitem:
        - button "整理已准备的上下文 · project_context"
        - paragraph: 校验并投影已准备的上下文，不在此重新检索或产生模型请求
        - paragraph: 节点类型：fn · 步骤 · 计算成功 · 已提交
      - listitem:
        - button "标记上下文不可用 · recover_context"
        - paragraph: 处理投影故障，不能解释为上下文恢复成功
        - paragraph: 节点类型：fn · 步骤 · 跳过 · 已提交 · all_inbound_not_taken
      - listitem:
        - button "决定处理分支 · join_route"
        - paragraph: 综合分类、守卫、Skills 与上下文情况选择分支
        - paragraph: 节点类型：fn · 步骤 · 计算成功 · 已提交
      - listitem:
        - button "完整处理 · full_agent"
        - paragraph: 进入图内 Agent 完成实质任务
        - paragraph: 节点类型：agent · 步骤 · 计算成功 · 已提交
      - listitem:
        - button "返回完整处理结果 · full_result"
        - paragraph: 结果出口，交付 Agent 的输出
        - paragraph: 节点类型：fn · 结果出口 · full_output · 计算成功 · 已提交
      - listitem:
        - button "返回固定短回复 · quick_result"
        - paragraph: 合格问候/感谢返回固定短回复，不是生成式回答
        - paragraph: 节点类型：fn · 结果出口 · quick_output · 跳过 · 已提交 · all_inbound_not_taken
      - listitem:
        - button "返回上下文失败提示 · context_failure_result"
        - paragraph: 结果出口，产生固定失败提示
        - paragraph: 节点类型：fn · 结果出口 · context_failure_output · 跳过 · 已提交 · all_inbound_not_taken
      - listitem:
        - button "按要求不回复 · no_reply_terminal"
        - paragraph: 无动作出口，保留用户记录，不生成助手消息或长期记忆
        - paragraph: 节点类型：fn · 无动作出口 · user_requested_no_reply（不产生内容） · 跳过 · 已提交 · all_inbound_not_taken
    - heading "全部连接与条件" [level=4]
    - list:
      - listitem: project_context → recover_context · 声明的恢复路径 · 未选中 not_taken
      - listitem: classify → join_route · 步骤依赖 · 已选中 taken
      - listitem: project_context → join_route · 步骤依赖 · 已选中 taken
      - listitem: recover_context → join_route · 步骤依赖 · 未选中 not_taken
      - listitem: full_agent → full_result · 步骤依赖 · 已选中 taken
      - listitem: join_route → quick_result · 条件分支 · quick：分类为问候/感谢、匹配有限短语且没有已加载 Skills，使用固定短回复 · 未选中 not_taken
      - listitem: join_route → full_agent · 条件分支 · full：分类要求完整处理 · 已选中 taken
      - listitem: join_route → full_agent · 条件分支 · fallback：分类未知、短语守卫不匹配或已有 Skills，改由图内完整处理 · 未选中 not_taken
      - listitem: join_route → no_reply_terminal · 条件分支 · no_action：分类为无需回复且匹配守卫；该判断先于上下文不可用和 Skills 分支 · 未选中 not_taken
      - listitem: join_route → context_failure_result · 条件分支 · context_failure：上下文不可用，返回固定提示 · 未选中 not_taken
    - paragraph: 图外策略：关闭分流走普通对话。图不可用或失败时，普通循环回退受副作用、上下文、安全停止和剩余预算等条件限制；图内 fallback 仅表示进入完整处理。
    - group: 快照身份与输入声明
    - heading "按 wave 分组" [level=4]
    - paragraph: Wave 0 · 已提交 · classify, project_context
    - paragraph: Wave 1 · 已提交 · recover_context
    - paragraph: Wave 2 · 已提交 · join_route
    - paragraph: Wave 3 · 已提交 · full_agent, quick_result, context_failure_result, no_reply_terminal
    - paragraph: Wave 4 · 已提交 · full_result
    - heading "独立业务摘要" [level=4]
    - region "独立业务摘要": "{ \"stages\": [], \"graph\": \"Completed\", \"fallback\": { \"decision\": \"not_needed\", \"entered\": false, \"model_requests\": 0 }, \"graph_error\": null, \"no_action\": null, \"recoveries\": null }"
    - group: 条件映射与读取声明
  - group: 用量
  - region "追踪导出":
    - heading "追踪导出" [level=2]
    - status
    - group:
      - text: 追踪导出操作
      - combobox "导出模式":
        - option "分享净化" [selected]
        - option "保留诊断正文"
      - paragraph: 默认移除自由正文；净化与源追踪完整性分别说明。
      - button "生成追踪导出"
- region "MainBar":
  - heading "主对话" [level=2]
  - button "收起主对话"
  - text: 会话
  - button "新建会话"
  - region "主对话":
    - article:
      - paragraph: 解释图执行
      - paragraph: 离线路由回复
      - text: 已保存
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
  4   | async function routingRun(page, server, message='解释图执行') {
  5   |   await page.goto(server.origin+'/behaviour');
  6   |   const enabled=page.getByRole('checkbox',{name:'启用消息分流'});
  7   |   await expect(enabled).toBeEnabled();
  8   |   await enabled.check();
  9   |   await page.getByRole('button',{name:'保存设置',exact:true}).click();
  10  |   await expect(page.getByText('已保存；下一 Run 生效。')).toBeVisible();
  11  |   const created=page.waitForResponse(r=>r.url().endsWith('/api/sessions')&&r.request().method()==='POST');
  12  |   await page.getByRole('button',{name:'新建会话',exact:true}).click();
  13  |   const session=(await (await created).json()).session_id;
  14  |   await expect.poll(()=>page.evaluate(()=>sessionStorage.getItem('alfred.session'))).toBe(session);
  15  |   if(await page.locator('#shell-toolbar [data-open-panel="mainbar"]').count()) await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  16  |   await page.getByRole('textbox',{name:'消息'}).fill(message);
  17  |   const accepted=page.waitForResponse(r=>r.url().endsWith('/api/runs')&&r.request().method()==='POST');
  18  |   await expect(page.getByRole('button',{name:'发送',exact:true})).toBeEnabled();
  19  |   await page.getByRole('button',{name:'发送',exact:true}).click();
  20  |   const response=await accepted;
  21  |   expect(response.status()).toBe(202);
  22  |   const identity=await response.json();
  23  |   await expect(page.locator('#messages').getByText('已保存',{exact:true})).toBeVisible();
  24  |   return identity.run_id;
  25  | }
  26  | 
  27  | test('AC-01/02/03 CE-10: actual routing path is collapsed and refresh is manual',async({page})=>{
  28  |   const server=await memoryServer({script:'tests/browser/routing_server.py'});
  29  |   try {
  30  |     const run=await routingRun(page,server);
  31  |     let reads=0;page.on('request',r=>{if(r.url().includes('/api/run-path?'))reads++;});
  32  |     await page.goto(server.origin+'/runs/'+run);
  33  |     const button=page.getByRole('button',{name:'本次执行路径',exact:true});
  34  |     await expect(button).toHaveAttribute('aria-expanded','false');
  35  |     expect(reads).toBe(0);
  36  |     await button.click();
  37  |     const panel=page.getByRole('region',{name:'本次执行路径',exact:true});
  38  |     await expect(panel.locator('svg [data-node-id]')).toHaveCount(9);
  39  |     await expect(panel).toContainText('持久 trace');
  40  |     await expect(panel).toContainText('已提交');
  41  |     expect(reads).toBe(1);
  42  |   } finally {await server.close();}
  43  | });
  44  | 
  45  | const region=page=>page.getByRole('region',{name:'本次执行路径',exact:true});
  46  | const pathRoute='**/api/run-path?*';
  47  | function barrier(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
  48  | async function openPath(page,origin,run){
  49  |   await page.goto(origin+'/runs/'+run);
  50  |   await expect(page.getByRole('button',{name:'新建会话',exact:true})).toBeEnabled();
  51  |   await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
  52  |   await expect(region(page).locator('svg [data-node-id]')).toHaveCount(9);
  53  |   return region(page);
  54  | }
  55  | async function rawPath(page,origin,run){return (await page.request.get(origin+'/api/run-path?run_id='+run)).json();}
  56  | 
  57  | for(const viewport of [{width:1280,height:850},{width:390,height:844}]) test(`CE-11/AC-19 keyboard graph/text equivalence ${viewport.width}`,async({page})=>{
  58  |   const server=await memoryServer({script:'tests/browser/routing_server.py'});
  59  |   try{
  60  |     const run=await routingRun(page,server);
  61  |     await page.setViewportSize(viewport);
  62  |     await page.goto(server.origin+'/runs/'+run);
  63  |     await expect(page.locator('#new-session')).toBeEnabled();
  64  |     await page.keyboard.press('Escape');
  65  |     const toggle=page.getByRole('button',{name:'本次执行路径',exact:true});
  66  |     await toggle.focus();await page.keyboard.press('Enter');
  67  |     const panel=region(page);
  68  |     await expect(panel.locator('svg [data-node-id]')).toHaveCount(9);
  69  |     const snapshot=await rawPath(page,server.origin,run);
  70  |     expect(await panel.locator('svg [data-node-id]').evaluateAll(els=>els.map(e=>e.getAttribute('data-node-id')).sort())).toEqual(snapshot.nodes.map(n=>n.node_id).sort());
  71  |     expect(await panel.locator('svg [data-graph-edge]').evaluateAll(els=>els.map(e=>JSON.parse(e.getAttribute('data-graph-edge'))))).toEqual(snapshot.description.topology.edges);
  72  |     expect(await panel.locator('[data-topology-edge]').evaluateAll(els=>els.map(e=>JSON.parse(e.getAttribute('data-topology-edge'))))).toEqual(snapshot.description.topology.edges);
  73  |     const select=panel.getByRole('button',{name:/ · classify$/});
  74  |     await select.focus();await page.keyboard.press('Enter');
  75  |     await expect(panel.getByRole('region',{name:'节点详情'})).toHaveCount(0); // detail uses a labelled div
  76  |     await expect(panel.locator('[aria-label="节点详情"]')).toContainText('Step');
  77  |     await panel.getByRole('button',{name:'放大',exact:true}).focus();await page.keyboard.press('Enter');
  78  |     const reference=panel.locator('[aria-label="节点详情"] a').first();
> 79  |     await expect(reference).toBeVisible();
      |                             ^ Error: expect(locator).toBeVisible() failed
  80  |     const target=await reference.getAttribute('href');
  81  |     await reference.focus();await page.keyboard.press('Enter');
  82  |     expect(await page.locator(target).count()).toBe(1);
  83  |     const view=await panel.locator('svg').getAttribute('viewBox');
  84  |     await panel.getByRole('button',{name:'重新读取',exact:true}).focus();await page.keyboard.press('Enter');
  85  |     await expect(panel).not.toContainText('重新读取中');
  86  |     expect(await panel.locator('svg').getAttribute('viewBox')).toBe(view);
  87  |     await expect(panel.locator('[aria-label="节点详情"]')).toContainText('classify');
  88  |     await panel.getByRole('button',{name:'收起执行路径',exact:true}).focus();await page.keyboard.press('Enter');
  89  |     await toggle.focus();await page.keyboard.press('Enter');
  90  |     await expect(panel.locator('svg')).toHaveAttribute('viewBox',view);
  91  |     await page.reload();await expect(toggle).toHaveAttribute('aria-expanded','false');
  92  |   }finally{await server.close();}
  93  | });
  94  | 
  95  | test('CE-08 latest request wins across reverse responses, reopen and Run navigation',async({page})=>{
  96  |   const server=await memoryServer({script:'tests/browser/routing_server.py'});
  97  |   try{
  98  |     const a=await routingRun(page,server);
  99  |     // The second read returns enabled=true only after the initial disabled,
  100 |     // unchecked control is visible; preserve that real loading boundary.
  101 |     const settingsSeen=barrier(),settingsRelease=barrier();
  102 |     await page.route('**/api/behaviour',async route=>{
  103 |       if(route.request().method()!=='GET')return route.continue();
  104 |       const response=await route.fetch();expect((await response.json()).enabled).toBe(true);
  105 |       settingsSeen.resolve();await settingsRelease.promise;await route.fulfill({response});
  106 |     });
  107 |     const second=routingRun(page,server,'不用回复');
  108 |     try{
  109 |       await settingsSeen.promise;
  110 |       await expect(page.getByRole('checkbox',{name:'启用消息分流'})).toBeDisabled();
  111 |       await expect(page.getByRole('checkbox',{name:'启用消息分流'})).not.toBeChecked();
  112 |     }finally{settingsRelease.resolve();}
  113 |     const b=await second;
  114 |     await page.unroute('**/api/behaviour');
  115 |     await openPath(page,server.origin,a);
  116 |     const held=barrier(),seen=barrier();let use=true;
  117 |     await page.route(pathRoute,async route=>{
  118 |       if(!use)return route.continue();use=false;
  119 |       const response=await route.fetch();seen.resolve();await held.promise;await route.fulfill({response});
  120 |     });
  121 |     await region(page).getByRole('button',{name:'重新读取',exact:true}).click();await seen.promise;
  122 |     await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
  123 |     await expect(region(page)).not.toContainText('重新读取中');
  124 |     await region(page).getByRole('button',{name:'收起执行路径',exact:true}).click();
  125 |     await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
  126 |     await expect(region(page)).not.toContainText('重新读取中');
  127 |     await openPath(page,server.origin,b);
  128 |     await expect(region(page)).toContainText('NoAction');
  129 |     held.resolve();
  130 |     await expect(region(page)).toContainText('NoAction');
  131 |     await expect(region(page).locator('[aria-label="节点详情"]')).toBeEmpty();
  132 |     await page.unroute(pathRoute);
  133 |   }finally{await server.close();}
  134 | });
  135 | 
  136 | test('CE-01/08: real process restart reads P1 retained graph and invalidates old response',async({page})=>{
  137 |   const server=await memoryServer({script:'tests/browser/run_path_server.py'});
  138 |   try{
  139 |     const run=await routingRun(page,server);
  140 |     const p1=await rawPath(page,server.origin,run);
  141 |     await openPath(page,server.origin,run);
  142 |     const held=barrier(),seen=barrier();let use=true;
  143 |     await page.route(pathRoute,async route=>{
  144 |       if(!use)return route.continue();use=false;
  145 |       const response=await route.fetch();seen.resolve();await held.promise;await route.fulfill({response});
  146 |     });
  147 |     await region(page).getByRole('button',{name:'重新读取',exact:true}).click();await seen.promise;
  148 |     await server.restart();
  149 |     await page.reload();
  150 |     await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
  151 |     await expect(region(page).locator('svg [data-node-id]')).toHaveCount(9);
  152 |     const p2=await rawPath(page,server.origin,run);
  153 |     expect(p2.identity).toEqual(p1.identity);
  154 |     expect(p2.service_instance_id).not.toBe(p1.service_instance_id);
  155 |     held.resolve();
  156 |     await region(page).getByText('快照身份与输入声明',{exact:true}).click();
  157 |     await expect(region(page)).toContainText(p2.service_instance_id);
  158 |     await expect(region(page)).toContainText(p1.identity.process_instance_id);
  159 |     await expect(region(page)).toContainText('持久 trace');
  160 |     await page.unroute(pathRoute);
  161 |   }finally{await server.close();}
  162 | });
  163 | 
  164 | test('CE-07/15 network failure keeps stale snapshot; verified missing clears it and recovery restores',async({page})=>{
  165 |   const {readdir,readFile,unlink,writeFile}=await import('node:fs/promises');
  166 |   const {join}=await import('node:path');
  167 |   const server=await memoryServer({script:'tests/browser/routing_server.py'});
  168 |   try{
  169 |     const run=await routingRun(page,server);
  170 |     await page.goto(server.origin+'/runs/'+run);
  171 |     await page.route(pathRoute,route=>route.abort());
  172 |     await page.getByRole('button',{name:'本次执行路径',exact:true}).click();
  173 |     await expect(region(page)).toContainText('首次读取失败');
  174 |     await page.unroute(pathRoute);
  175 |     await region(page).getByRole('button',{name:'重试读取',exact:true}).click();
  176 |     await expect(region(page).locator('svg [data-node-id]')).toHaveCount(9);
  177 |     await page.route(pathRoute,route=>route.abort());
  178 |     await region(page).getByRole('button',{name:'重新读取',exact:true}).click();
  179 |     await expect(region(page)).toContainText('旧快照 / 本次读取失败');
```