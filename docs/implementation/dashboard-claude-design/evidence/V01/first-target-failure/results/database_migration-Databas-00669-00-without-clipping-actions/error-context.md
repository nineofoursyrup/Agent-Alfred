# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: database_migration.spec.js >> Database measures central 679/680 and shell 1099/1100 without clipping actions
- Location: tests/browser/database_migration.spec.js:153:1

# Error details

```
Error: expect(received).toBe(expected) // Object.is equality

Expected: 232
Received: 0
```

# Page snapshot

```yaml
- generic [ref=e1]:
  - banner "导航面板" [ref=e2]:
    - link "Agent-Alfred 本地助手" [ref=e3] [cursor=pointer]:
      - /url: /overview
      - text: Agent-Alfred
      - generic [ref=e4]: 本地助手
    - navigation "主导航" [ref=e5]:
      - paragraph [ref=e6]: 工作区
      - link "总览" [ref=e7] [cursor=pointer]:
        - /url: /overview
      - link "收件箱" [ref=e8] [cursor=pointer]:
        - /url: /inbox
      - link "运行" [ref=e9] [cursor=pointer]:
        - /url: /runs
      - link "记忆" [ref=e10] [cursor=pointer]:
        - /url: /memory
      - paragraph [ref=e11]: Agent 配置
      - link "模型" [ref=e12] [cursor=pointer]:
        - /url: /models
      - link "连接" [ref=e13] [cursor=pointer]:
        - /url: /connections
      - link "行为" [ref=e14] [cursor=pointer]:
        - /url: /behaviour
      - link "工具" [ref=e15] [cursor=pointer]:
        - /url: /tools
      - paragraph [ref=e16]: 诊断
      - link "用量账本" [ref=e17] [cursor=pointer]:
        - /url: /ops
      - link "数据库" [ref=e18] [cursor=pointer]:
        - /url: /database
    - paragraph [ref=e19]: 本地优先 · 共享运行宿主
  - generic [ref=e20]:
    - generic "壳层状态与操作" [ref=e21]:
      - button "打开主对话" [expanded] [ref=e22] [cursor=pointer]
      - region "运行与连接摘要" [ref=e23]:
        - paragraph [ref=e24]: 尚未选择会话
    - main [ref=e25]:
      - heading "数据库" [active] [level=1] [ref=e26]
      - generic [ref=e27]:
        - paragraph [ref=e28]: 查询受保护的临时数据。SQL 的 WHERE／LIMIT 不缩小准备范围。结果有上限，不是数据库总量。
        - region "诊断对象目录" [ref=e29]:
          - paragraph [ref=e30]: 当前实例固定。记忆修订 20 · 保护规则版本 5
          - paragraph [ref=e31]: 可用性：已核验可用
          - paragraph [ref=e32]: 执行预算 5 秒 · 最多 1000 行／64 列／2 MiB JSON
          - group [ref=e33]:
            - generic "对象目录" [ref=e34] [cursor=pointer]
          - group [ref=e35]:
            - generic "完整限制与寿命" [ref=e36] [cursor=pointer]
        - generic [ref=e37]: SQL 草稿
        - textbox "SQL" [ref=e38]:
          - /placeholder: SELECT * FROM diag_sessions LIMIT 20
        - paragraph [ref=e39]: Enter 换行。仅点击执行才提交；查询中可编辑下一份草稿。
        - generic [ref=e40]:
          - button "执行" [ref=e41] [cursor=pointer]
          - button "取消" [disabled] [ref=e42]
          - button "核验查询与可用性" [ref=e43] [cursor=pointer]
          - button "复制 SQL" [disabled] [ref=e44]
        - paragraph [ref=e45]: 记忆或保护版本变化：已清除结果及提交关联；草稿保留，核验后仍需显式执行。
        - region "查询与资源状态" [ref=e46]:
          - generic [ref=e47]:
            - heading "执行" [level=3] [ref=e48]
            - paragraph [ref=e49]: 可执行
          - generic [ref=e50]:
            - heading "正文" [level=3] [ref=e51]
            - paragraph [ref=e52]: 尚未收到查询正文
          - generic [ref=e53]:
            - heading "清理" [level=3] [ref=e54]
            - paragraph [ref=e55]: 尚无查询清理责任
        - region "已提交查询"
        - button "复制本页结果" [disabled] [ref=e56]
        - status [ref=e57]: 尚无有效结果可复制。
        - region "查询结果"
  - region "MainBar" [ref=e58]:
    - generic [ref=e59]:
      - heading "主对话" [level=2] [ref=e60]
      - button "收起主对话" [ref=e61] [cursor=pointer]
    - generic [ref=e62]:
      - generic [ref=e63]: 尚未选择会话
      - button "新建会话" [ref=e64] [cursor=pointer]
    - region "主对话" [ref=e65]
    - generic [ref=e66]:
      - generic [ref=e67]: 消息
      - textbox "消息" [disabled] [ref=e68]:
        - /placeholder: 写下消息；/skills 指定流程…
      - button "发送" [disabled] [ref=e69]
    - paragraph [ref=e70]: 请显式新建或继续会话。
  - status [ref=e71]
  - alert [ref=e72]
```

# Test source

```ts
  75  |       const box = await button.boundingBox();
  76  |       expect(box.x).toBeGreaterThanOrEqual(0);
  77  |       expect(box.x+box.width).toBeLessThanOrEqual(width);
  78  |     }
  79  |     if (width < 1100) {
  80  |       await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  81  |       await expect(editor(page)).not.toBeVisible();
  82  |       await expect(page.locator('[aria-label="查询结果"] td').last()).toHaveText(JSON.stringify(long));
  83  |       await page.getByRole('button',{name:'收起主对话',exact:true}).click();
  84  |       await expect(editor(page)).toHaveValue(/LAST-VALUE/);
  85  |     }
  86  |     const metrics = await page.evaluate(() => {
  87  |       const style = selector => {
  88  |         const computed = getComputedStyle(document.querySelector(selector));
  89  |         return {fontSize:computed.fontSize,color:computed.color,background:computed.backgroundColor};
  90  |       };
  91  |       return {viewport:[innerWidth,innerHeight], dpr:devicePixelRatio,
  92  |         central:document.querySelector('.page-body').getBoundingClientRect().width,
  93  |         body:style('.db-console'), table:style('.db-console table'), meta:style('.db-result-meta'),
  94  |         activeAnimations:document.getAnimations().length, browser:navigator.userAgent,
  95  |         maxTouchPoints:navigator.maxTouchPoints,
  96  |         scope:innerWidth<1100?'Chromium viewport simulation, no real mobile keyboard':'Chromium desktop viewport'};
  97  |     });
  98  |     expect(metrics.maxTouchPoints > 0).toBe(width < 1100);
  99  |     expect(metrics.body.fontSize).toBe('13px');
  100 |     expect(metrics.table.fontSize).toBe('13px');
  101 |     expect(metrics.meta.fontSize).toBe('11px');
  102 |     expect(metrics.activeAnimations).toBe(0);
  103 |     const metricFile = testInfo.outputPath('geometry-and-environment.json');
  104 |     await writeFile(metricFile,JSON.stringify(metrics,null,2));
  105 |     await testInfo.attach('geometry-and-environment', {path:metricFile,contentType:'application/json'});
  106 |     await page.locator('#page').evaluate(el => el.scrollTop=0);
  107 |     await page.screenshot({path:testInfo.outputPath(`database-${width}x${height}-top.png`)});
  108 |     await result(page).scrollIntoViewIfNeeded();
  109 |     await page.screenshot({path:testInfo.outputPath(`database-${width}x${height}-long-result.png`)});
  110 |   });
  111 |   });
  112 | }
  113 | 
  114 | test('Database row, column and byte results follow actual worker boundaries', async ({page},testInfo) => {
  115 |   await open(page);
  116 |   await editor(page).fill('SELECT '+Array.from({length:64},(_,i)=>`${i} AS col_${i}`).join(','));
  117 |   await run(page).click();
  118 |   await expect(result(page).locator('th')).toHaveCount(64);
  119 |   await expect(result(page).locator('th').last()).toHaveText('col_63');
  120 |   await editor(page).fill('SELECT '+Array.from({length:65},(_,i)=>i).join(','));
  121 |   const rejected = page.waitForResponse(r=>r.url().endsWith('/execute'));
  122 |   await run(page).click();
  123 |   expect((await rejected).status()).toBe(400);
  124 |   await expect(page.locator('[data-state]')).toContainText('请求被拒绝');
  125 |   await expect(result(page)).toBeEmpty();
  126 |   await page.screenshot({path:testInfo.outputPath('database-65-columns-rejected.png')});
  127 |   const values = Array.from({length:11},(_,i)=>`SELECT ${i} AS n`).join(' UNION ALL ');
  128 |   const long = 'x'.repeat(50000)+'BYTE-END';
  129 |   await editor(page).fill(`WITH digits AS (${values}) SELECT '${long}' AS body FROM digits a CROSS JOIN digits b`);
  130 |   const completed = page.waitForResponse(r=>r.url().endsWith('/execute'));
  131 |   await run(page).click();
  132 |   const response = await completed;
  133 |   const body = await response.json();
  134 |   expect(response.status()).toBe(200);
  135 |   expect(body.truncation_reasons).toEqual(['bytes']);
  136 |   expect(body.returned_rows).toBeLessThan(121);
  137 |   expect(Buffer.byteLength(await response.text())).toBeLessThanOrEqual(2097152);
  138 |   await expect(page.locator('[data-state]')).toHaveText('结果截断 · 字节上限');
  139 |   await expect(result(page)).toContainText('字节上限 (bytes)');
  140 |   await expect(result(page).locator('td').first()).toHaveText(JSON.stringify(long));
  141 | });
  142 | 
  143 | test('Database failed clipboard writes give local feedback and unavailable results cannot copy', async ({page}) => {
  144 |   await open(page);
  145 |   await expect(page.getByRole('button',{name:'复制本页结果',exact:true})).toBeDisabled();
  146 |   await editor(page).fill('SELECT 1');
  147 |   // Clipboard is an external OS permission boundary. The query path remains real.
  148 |   await page.evaluate(() => navigator.clipboard.writeText = async () => {throw new DOMException('denied','NotAllowedError');});
  149 |   await page.getByRole('button',{name:'复制 SQL',exact:true}).click();
  150 |   await expect(page.getByText('复制失败；请检查剪贴板权限后重试。',{exact:true})).toBeVisible();
  151 | });
  152 | 
  153 | test('Database measures central 679/680 and shell 1099/1100 without clipping actions', async ({page},testInfo) => {
  154 |   await open(page);
  155 |   const evidence = [];
  156 |   for (const target of [679,680]) {
  157 |     await page.setViewportSize({width:target+32,height:800});
  158 |     const actual = await page.locator('.page-body').evaluate(el=>el.getBoundingClientRect().width);
  159 |     await page.setViewportSize({width:target+32+(target-actual),height:800});
  160 |     const central = await page.locator('.page-body').evaluate(el=>el.getBoundingClientRect().width);
  161 |     expect(central).toBe(target);
  162 |     const columns = await page.locator('.db-facts').evaluate(el=>getComputedStyle(el).gridTemplateColumns.split(' ').length);
  163 |     expect(columns).toBe(target<680?1:3);
  164 |     evidence.push({target,central,columns,viewport:page.viewportSize()});
  165 |     await page.screenshot({path:testInfo.outputPath(`database-central-${target}.png`)});
  166 |   }
  167 |   for (const width of [1099,1100]) {
  168 |     await page.setViewportSize({width,height:800});
  169 |     const geometry = await page.evaluate(()=>({viewport:innerWidth,
  170 |       central:document.querySelector('.page-body').getBoundingClientRect().width,
  171 |       rail:document.querySelector('.site-header').getBoundingClientRect().width,
  172 |       mainbar:document.querySelector('#mainbar').getBoundingClientRect().width,
  173 |       clipped:document.documentElement.scrollWidth>innerWidth}));
  174 |     expect(geometry.clipped).toBe(false);
> 175 |     if(width===1100){expect(geometry.rail).toBe(232);expect(geometry.mainbar).toBe(320);}
      |                                            ^ Error: expect(received).toBe(expected) // Object.is equality
  176 |     evidence.push(geometry);
  177 |     await page.screenshot({path:testInfo.outputPath(`database-shell-${width}.png`)});
  178 |   }
  179 |   const metricFile = testInfo.outputPath('actual-breakpoints.json');
  180 |   await writeFile(metricFile,JSON.stringify(evidence,null,2));
  181 |   await testInfo.attach('actual-breakpoints', {path:metricFile,contentType:'application/json'});
  182 | });
  183 | 
```