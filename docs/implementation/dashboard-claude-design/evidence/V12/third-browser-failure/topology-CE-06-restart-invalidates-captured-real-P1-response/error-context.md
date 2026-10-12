# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: topology.spec.js >> CE-06: restart invalidates captured real P1 response
- Location: tests/browser/topology.spec.js:202:3

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: getByRole('region', { name: '消息分流', exact: true }).getByText(/来自上一服务实例/)
Expected: visible
Timeout: 4000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" getByRole('region', { name: '消息分流', exact: true }).getByText(/来自上一服务实例/) with timeout 4000ms
  - waiting for getByRole('region', { name: '消息分流', exact: true }).getByText(/来自上一服务实例/)

```

```yaml
- banner "导航面板":
  - link "Alfred 本地助手":
    - /url: /overview
  - paragraph: 连接中断或正在同步
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
  - paragraph: BEHAVIOUR
  - heading "行为" [level=1]
  - paragraph: 查看与配置消息分流，区分流程拓扑、路由决策与执行事实。
  - button "打开主对话" [expanded]
  - region "运行与连接摘要":
    - paragraph: 连接中断或正在同步
  - region "消息分流":
    - heading "消息分流" [level=2]
    - paragraph: 持续设置 · CLI/Web 共享。默认关闭；保存后下一 Run 生效。回复进入当前会话，静默时只记用户消息，不生成助手消息或长期记忆。
    - paragraph: 已保存的分流设置：关闭 · revision 0。能查看结构不代表已经启用或可以成功执行。
    - paragraph: 启用后识别纯问候、致谢及明确无需回复的消息；实际任务仍进入完整回答。
    - checkbox "启用消息分流"
    - text: 启用消息分流
    - paragraph: 当前选择：关闭 · 编辑基线 revision 0。
    - button "保存设置"
    - button "刷新设置"
    - status: 当前值已核对；设置在下一 Run 生效。
    - region "流程拓扑":
      - button "收起流程" [expanded]
      - status: 服务连接已断开；保留的图仅为旧快照。
      - status: 读取时的结构 · 2026-10-12T07:16:54Z。旧快照 / 本次读取失败，请重新读取。
      - button "重新读取"
      - button "放大"
      - button "缩小"
      - button "适应全图"
      - button "向左平移"
      - button "向右平移"
      - button "向上平移"
      - button "向下平移"
      - paragraph: 图例：实线＝步骤依赖；虚线＝条件分支；点线＝声明的恢复路径（不保证任意异常均可恢复）。出口以文字标明。
      - img "流程节点与连接图": quick full fallback no_action context_failure 判断消息类型 classify llm · 步骤 整理已准备的上下文 project_context fn · 步骤 标记上下文不可用 recover_context fn · 步骤 决定处理分支 join_route fn · 步骤 完整处理 full_agent agent · 步骤 返回完整处理结果 full_result fn · 结果出口 返回固定短回复 quick_result fn · 结果出口 返回上下文失败提示 context_failure_result fn · 结果出口 按要求不回复 no_reply_terminal fn · 无动作出口
      - heading "完整文字明细" [level=3]
      - paragraph: 这是声明结构，不代表某次运行已经执行、成功或跳过。图存在不承诺模型/工具可用或下一次运行成功。
      - list:
        - listitem:
          - button "判断消息类型 · classify"
          - paragraph: 仅分类当前任务，分类是控制信息；它不生成用户回复
          - paragraph: 节点类型：llm · 步骤
        - listitem:
          - button "整理已准备的上下文 · project_context"
          - paragraph: 校验并投影已准备的上下文，不在此重新检索或产生模型请求
          - paragraph: 节点类型：fn · 步骤
        - listitem:
          - button "标记上下文不可用 · recover_context"
          - paragraph: 处理投影故障，不能解释为上下文恢复成功
          - paragraph: 节点类型：fn · 步骤
        - listitem:
          - button "决定处理分支 · join_route"
          - paragraph: 综合分类、守卫、Skills 与上下文情况选择分支
          - paragraph: 节点类型：fn · 步骤
        - listitem:
          - button "完整处理 · full_agent"
          - paragraph: 进入图内 Agent 完成实质任务
          - paragraph: 节点类型：agent · 步骤
        - listitem:
          - button "返回完整处理结果 · full_result"
          - paragraph: 结果出口，交付 Agent 的输出
          - paragraph: 节点类型：fn · 结果出口 · full_output
        - listitem:
          - button "返回固定短回复 · quick_result"
          - paragraph: 合格问候/感谢返回固定短回复，不是生成式回答
          - paragraph: 节点类型：fn · 结果出口 · quick_output
        - listitem:
          - button "返回上下文失败提示 · context_failure_result"
          - paragraph: 结果出口，产生固定失败提示
          - paragraph: 节点类型：fn · 结果出口 · context_failure_output
        - listitem:
          - button "按要求不回复 · no_reply_terminal"
          - paragraph: 无动作出口，保留用户记录，不生成助手消息或长期记忆
          - paragraph: 节点类型：fn · 无动作出口 · user_requested_no_reply（不产生内容）
      - heading "全部连接与条件" [level=4]
      - list:
        - listitem: project_context → recover_context · 声明的恢复路径
        - listitem: classify → join_route · 步骤依赖
        - listitem: project_context → join_route · 步骤依赖
        - listitem: recover_context → join_route · 步骤依赖
        - listitem: full_agent → full_result · 步骤依赖
        - listitem: join_route → quick_result · 条件分支 · quick：分类为问候/感谢、匹配有限短语且没有已加载 Skills，使用固定短回复
        - listitem: join_route → full_agent · 条件分支 · full：分类要求完整处理
        - listitem: join_route → full_agent · 条件分支 · fallback：分类未知、短语守卫不匹配或已有 Skills，改由图内完整处理
        - listitem: join_route → no_reply_terminal · 条件分支 · no_action：分类为无需回复且匹配守卫；该判断先于上下文不可用和 Skills 分支
        - listitem: join_route → context_failure_result · 条件分支 · context_failure：上下文不可用，返回固定提示
      - paragraph: 图外策略：关闭分流走普通对话。图不可用或失败时，普通循环回退受副作用、上下文、安全停止和剩余预算等条件限制；图内 fallback 仅表示进入完整处理。
      - group:
        - text: 快照身份与输入声明
        - region "快照身份与输入声明": "{ \"workflow\": \"message_routing\", \"graph_id\": \"message_routing\", \"process_instance_id\": \"499ad692b6934eddb96677b0b0558396\", \"publication_generation\": 2, \"schema_version\": 1, \"topology_hash\": \"18ed11bdc25316b2bbc182e6b43f3e601fd9011add9a734e818926dc628bbb1f\", \"inputs\": { \"task\": true, \"prepared_context\": true, \"has_loaded_skills\": true } }"
      - group: 条件映射与读取声明
    - group:
      - text: 路由统计
      - status: 连接已中断，请刷新统计。
  - region "手动聚合":
    - heading "手动聚合" [level=2]
    - paragraph: 单次动作 · 显式生成时固定目标会话与所选来源，草稿进入目标会话。草稿不自动进入工作窗口、后续聚合或提炼；再次生成会创建新运行。
    - text: 目标会话
    - combobox "目标会话":
      - option "请选择目标会话" [selected]
    - text: 聚合目标
    - textbox "聚合目标": 保留草稿
    - text: 聚合关键词
    - textbox "聚合关键词": 保留草稿
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
    - paragraph: 表单有未提交输入；更改不会影响已提交的请求。
    - status: 本次读取 0 个会话。刷新列表不会改变已提交目标。
    - paragraph: 连接未同步；可保留表单，恢复连接后再生成。
    - status: 尚未提交聚合请求。
    - region "流程拓扑":
      - button "查看流程"
      - status: 服务连接已断开；保留的图仅为旧快照。
      - status: 尚未读取；展开后读取。
- region "MainBar":
  - heading "主对话" [level=2]
  - button "收起主对话"
  - complementary "本标签页连接状态":
    - paragraph: 连接已断开；已清除临时文字，正在重新连接。
    - text: 仅影响本标签页
    - button "关闭连接提示"
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
  126 |     await region.getByRole('button',{name:'放大',exact:true}).click();
  127 |     const viewport=await region.locator('svg').getAttribute('viewBox');
  128 |     await region.getByRole('button',{name:'收起流程',exact:true}).click();
  129 |     await region.getByRole('button',{name:'查看流程',exact:true}).click();
  130 |     await expect(region.getByText(/重新读取中/)).toHaveCount(0);
  131 |     expect(await region.locator('svg').getAttribute('viewBox')).toBe(viewport);
  132 |     await expect(region.getByRole('heading',{name:'判断消息类型 · classify'})).toBeVisible();
  133 |     const first=await identity(region);
  134 |     await server.send('publish-baseline');
  135 |     // Publishing never replaces an open reader or polls for another graph.
  136 |     expect(await identity(region)).toEqual(first);
  137 |     await region.getByRole('button',{name:'重新读取',exact:true}).click();
  138 |     await expect(region.getByText(/重新读取中/)).toHaveCount(0);
  139 |     expect((await identity(region)).publication_generation).toBeGreaterThan(first.publication_generation);
  140 |     expect(await region.locator('svg').getAttribute('viewBox')).toBe(viewport);
  141 |     await expect(region.getByRole('heading',{name:'判断消息类型 · classify'})).toBeVisible();
  142 |     await interceptOnce(page,routePath,async route=>{
  143 |       const response=await route.fetch();captured.release();await release.promise;
  144 |       await route.fulfill({response}).catch(()=>{});
  145 |     });
  146 |     await region.getByRole('button',{name:'重新读取',exact:true}).click();await captured.promise;
  147 |     await expect(region.getByText(/旧快照 · 重新读取中/)).toBeVisible();
  148 |     await server.send('publish-alternate');
  149 |     await region.getByRole('button',{name:'重新读取',exact:true}).click();
  150 |     await expect(region.locator('svg [data-node-id]')).toHaveCount(2);
  151 |     const latest=await identity(region);
  152 |     expect(latest.publication_generation).toBeGreaterThan(first.publication_generation);
  153 |     await expect(region.getByText(/结构已更新/)).toBeVisible();
  154 |     await expect(region.getByRole('heading',{name:'判断消息类型 · classify'})).toHaveCount(0);
  155 |     expect(await region.locator('svg').getAttribute('viewBox')).not.toBe(viewport);
  156 |     await expect(region.getByRole('button',{name:'future_step · future_step'})).toBeVisible();
  157 |     await expect(region.locator("li").filter({hasText:/future_branch：暂无说明/})).toBeVisible();
  158 |     release.release(); await page.unrouteAll({behavior:'wait'});
  159 |     expect(await identity(region)).toEqual(latest);
  160 |     await server.send('counts');
  161 |     const {readFile}=await import('node:fs/promises');
  162 |     expect(JSON.parse(await readFile(server.directory+'/counts.json','utf8')).model_requests).toBe(0);
  163 |   } finally {release.release();await server.close();}
  164 | });
  165 |
  166 | test('CE-03/04: transport failure, initial retry, independent graphs and real startup absence',async({page})=>{
  167 |   const server=await memoryServer({script:'tests/browser/topology_server.py'});
  168 |   try {
  169 |     await page.goto(server.origin+'/behaviour');
  170 |     const region=section(page);
  171 |     await page.route(routePath,route=>route.abort());
  172 |     await region.getByRole('button',{name:'查看流程',exact:true}).click();
  173 |     await expect(region.getByText('首次读取失败，请重试读取。')).toBeVisible();
  174 |     await page.unroute(routePath);
  175 |     await region.getByRole('button',{name:'重试读取',exact:true}).click();
  176 |     await expect(region.locator('svg [data-node-id]')).toHaveCount(9);
  177 |     const old=await identity(region);
  178 |     await server.send('publish-alternate');
  179 |     await page.route(routePath,route=>route.fulfill({status:503,json:{code:'topology_read_failed'}}));
  180 |     await region.getByRole('button',{name:'重新读取',exact:true}).click();
  181 |     await expect(region.getByText(/旧快照 \/ 本次读取失败/)).toBeVisible();
  182 |     await expect(region.locator('svg [data-node-id]')).toHaveCount(9);
  183 |     expect(await identity(region)).toEqual(old);
  184 |     const agg=page.getByRole('region',{name:'手动聚合',exact:true});
  185 |     await agg.getByRole('button',{name:'查看流程',exact:true}).click();
  186 |     await expect(agg.locator('svg [data-node-id]')).toHaveCount(15);
  187 |     await page.unroute(routePath);
  188 |     const {writeFile}=await import('node:fs/promises');
  189 |     await writeFile(server.directory+'/graph-mode','missing');
  190 |     await server.restart();
  191 |     await expect(region.getByText(/来自上一服务实例/)).toBeVisible();
  192 |     await region.getByRole('button',{name:'重新读取',exact:true}).click();
  193 |     await expect(region.getByText(/当前没有已发布的图/)).toBeVisible();
  194 |     await expect(region.locator('svg')).toHaveCount(0);
  195 |     await server.send('publish-baseline');
  196 |     await region.getByRole('button',{name:'重新读取',exact:true}).click();
  197 |     await expect(region.locator('svg [data-node-id]')).toHaveCount(9);
  198 |   } finally {await server.close();}
  199 | });
  200 |
  201 | for (const action of ['collapse','navigate','restart']) {
  202 |   test(`CE-06: ${action} invalidates captured real P1 response`,async({page})=>{
  203 |     const server=await memoryServer({script:'tests/browser/topology_server.py'});
  204 |     const captured=barrier(),release=barrier();
  205 |     try {
  206 |       let region=await openGraph(page,server);const first=await identity(region);
  207 |       await page.getByRole('textbox',{name:'聚合目标'}).fill('保留草稿');
  208 |       await interceptOnce(page,routePath,async route=>{const response=await route.fetch();captured.release();await release.promise;await route.fulfill({response}).catch(()=>{});});
  209 |       await region.getByRole('button',{name:'重新读取',exact:true}).click();await captured.promise;
  210 |       if(action==='collapse') {
  211 |         await region.getByRole('button',{name:'收起流程',exact:true}).click();
  212 |         await server.send('publish-alternate');
  213 |         await region.getByRole('button',{name:'查看流程',exact:true}).click();
  214 |         await expect(region.locator('svg [data-node-id]')).toHaveCount(2);
  215 |       } else if(action==='navigate') {
  216 |         await page.getByRole('link',{name:'运行',exact:true}).click();
  217 |         await page.getByRole('button',{name:'放弃并离开',exact:true}).click();
  218 |         release.release();await page.unrouteAll({behavior:'wait'});
  219 |         await expect(page.getByRole('region',{name:'流程拓扑'})).toHaveCount(0);
  220 |         await page.getByRole('link',{name:'行为',exact:true}).click();
  221 |         region=section(page);
  222 |         await expect(region.getByRole('button',{name:'查看流程'})).toHaveAttribute('aria-expanded','false');
  223 |         return;
  224 |       } else {
  225 |         await server.restart();
> 226 |         await expect(region.getByText(/来自上一服务实例/)).toBeVisible();
      |                                                    ^ Error: expect(locator).toBeVisible() failed
  227 |         await region.getByRole('button',{name:'重新读取',exact:true}).click();
  228 |         await expect(region.getByText(/来自上一服务实例/)).toHaveCount(0);
  229 |       }
  230 |       const latest=await identity(region);
  231 |       if(action==='restart') {expect(latest.process_instance_id).not.toBe(first.process_instance_id);expect(latest.topology_hash).toBe(first.topology_hash);}
  232 |       release.release();await page.unrouteAll({behavior:'wait'});
  233 |       expect(await identity(region)).toEqual(latest);
  234 |       await expect(page.getByRole('textbox',{name:'聚合目标'})).toHaveValue('保留草稿');
  235 |     } finally {release.release();await server.close();}
  236 |   });
  237 | }
  238 |
  239 | for (const width of [1280,390]) {
  240 |   test(`CE-08: keyboard and complete text at ${width}px`,async({page})=>{
  241 |     const server=await memoryServer({script:'tests/browser/topology_server.py'});
  242 |     try {
  243 |       await page.setViewportSize({width,height:850});
  244 |       await page.goto(server.origin+'/behaviour');
  245 |       for (const name of ['消息分流','手动聚合']) {
  246 |         const region=page.getByRole('region',{name,exact:true});
  247 |         const open=region.getByRole('button',{name:'查看流程',exact:true});
  248 |         await open.focus();await page.keyboard.press('Enter');
  249 |         const count=name==='消息分流'?9:15;
  250 |         await expect(region.locator('svg [data-node-id]')).toHaveCount(count);
  251 |         await region.locator('svg').screenshot({path:`output/playwright/topology-canvas-${name==='消息分流'?'routing':'aggregation'}-${width}.png`});
  252 |         // Tab from the toggle reaches refresh and every viewport control.
  253 |         await page.keyboard.press('Tab');
  254 |         await expect(region.getByRole('button',{name:'重新读取',exact:true})).toBeFocused();
  255 |         await page.keyboard.press('Enter');await expect(region.getByText(/重新读取中/)).toHaveCount(0);
  256 |         await page.keyboard.press('Tab');await expect(region.getByRole('button',{name:'放大',exact:true})).toBeFocused();
  257 |         const initial=await region.locator('svg').getAttribute('viewBox');await page.keyboard.press('Enter');
  258 |         expect(await region.locator('svg').getAttribute('viewBox')).not.toBe(initial);
  259 |         await page.keyboard.press('Tab');await page.keyboard.press('Tab');
  260 |         await expect(region.getByRole('button',{name:'适应全图',exact:true})).toBeFocused();await page.keyboard.press('Enter');
  261 |         expect(await region.locator('svg').getAttribute('viewBox')).toBe(initial);
  262 |         // Every node is selectable in text, without interacting with the SVG.
  263 |         const buttons=region.locator('li button');
  264 |         for(let i=0;i<await buttons.count();i++) {
  265 |           const b=buttons.nth(i);await b.focus();await page.keyboard.press('Enter');
  266 |           await expect(region.getByRole('heading',{name:await b.textContent(),exact:true})).toBeVisible();
  267 |         }
  268 |         const edges=region.locator('[data-topology-edge]');
  269 |         await expect(edges).toHaveCount(name==='消息分流'?10:20);
  270 |         for(const item of await edges.all()) {await item.scrollIntoViewIfNeeded();await expect(item).toBeVisible();}
  271 |         const overflow=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,elements:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>({tag:e.tagName,cls:e.className,text:e.textContent?.slice(0,70),right:e.getBoundingClientRect().right})).slice(0,12)}));
  272 |         expect(overflow.scroll,JSON.stringify(overflow)).toBeLessThanOrEqual(overflow.width);
  273 |         await page.screenshot({path:`output/playwright/topology-${name==='消息分流'?'routing':'aggregation'}-${width}.png`,fullPage:true});
  274 |         const close=region.getByRole('button',{name:'收起流程',exact:true});await close.focus();await page.keyboard.press('Enter');
  275 |         await expect(region.locator('svg')).toBeHidden();
  276 |       }
  277 |     } finally {await server.close();}
  278 |   });
  279 | }
  280 |
  281 | test('CE-01/03: disconnection and recovery never replace a snapshot or save drafts',async({page})=>{
  282 |   const server=await memoryServer({script:'tests/browser/topology_server.py'});
  283 |   try {
  284 |     let reads=0;
  285 |     page.on('request',r=>{if(r.url().includes('/api/behaviour/topology')) reads++;});
  286 |     const region=await openGraph(page,server);const first=await identity(region);
  287 |     await page.getByRole('checkbox',{name:'启用消息分流'}).check();
  288 |     await page.evaluate(()=>window.dispatchEvent(new Event('offline')));
  289 |     await expect(region.getByText(/服务连接已断开/)).toBeVisible();
  290 |     await server.send('publish-alternate');
  291 |     await page.evaluate(()=>window.dispatchEvent(new Event('online')));
  292 |     await expect(region.getByText(/服务已连接/)).toBeVisible();
  293 |     expect(await identity(region)).toEqual(first);expect(reads).toBe(1);
  294 |     await expect(page.getByRole('checkbox',{name:'启用消息分流'})).toBeChecked();
  295 |     await region.getByRole('button',{name:'重新读取',exact:true}).click();
  296 |     await expect(region.locator('svg [data-node-id]')).toHaveCount(2);expect(reads).toBe(2);
  297 |   } finally {await server.close();}
  298 | });
  299 |
  300 | test('AC-14: protocol text injection is shown literally and never becomes markup',async({page})=>{
  301 |   const server=await memoryServer({script:'tests/browser/topology_server.py'});
  302 |   try {
  303 |     const region=await openGraph(page,server);
  304 |     const literal='<img src=x onerror="window.topologyInjected=true">';
  305 |     await page.route(routePath,async route=>{
  306 |       const response=await route.fetch(),body=await response.json();
  307 |       body.description.presentation.nodes.classify.description=literal;
  308 |       body.description.presentation.branches.join_route.quick=literal;
  309 |       await route.fulfill({response,json:body});
  310 |     });
  311 |     await region.getByRole('button',{name:'重新读取',exact:true}).click();
  312 |     await expect(region.getByText(literal,{exact:true})).toBeVisible();
  313 |     expect(await region.locator('img').count()).toBe(0);
  314 |     expect(await page.evaluate(()=>window.topologyInjected)).toBeUndefined();
  315 |   } finally {await server.close();}
  316 | });
  317 |
  318 | test('CE-06: first shell synchronization precedes and preserves the first graph read',async({page})=>{
  319 |   const server=await memoryServer({script:'tests/browser/topology_server.py'});
  320 |   const entry=barrier(), graph=barrier(), captured=barrier();
  321 |   try {
  322 |     await interceptOnce(page,'**/api/entry',async route=>{const response=await route.fetch();await entry.promise;await route.fulfill({response});});
  323 |     await interceptOnce(page,routePath,async route=>{const response=await route.fetch();captured.release();await graph.promise;await route.fulfill({response}).catch(()=>{});});
  324 |     await page.goto(server.origin+'/behaviour',{waitUntil:'commit'});
  325 |     const region=section(page);
  326 |     await expect(region).toHaveCount(0); // Shell owns the first Host synchronization.
```
