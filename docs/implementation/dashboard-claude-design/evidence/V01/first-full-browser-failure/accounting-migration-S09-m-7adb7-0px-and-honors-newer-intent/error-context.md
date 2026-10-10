# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: accounting-migration.spec.js >> S09 migrated Run explicit return restores its visible source at 1280px and honors newer intent
- Location: tests/browser/accounting-migration.spec.js:230:3

# Error details

```
Error: expect(received).toBeCloseTo(expected, precision)

Expected: 362.578125
Received: 362.078125

Expected precision:    0
Expected difference: < 0.5
Received difference:   0.5
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
        - paragraph [ref=e24]: 连接中断或正在同步
    - main [ref=e25]:
      - heading "用量账本" [level=1] [ref=e26]
      - generic [ref=e27]:
        - form "账目筛选" [ref=e28]:
          - heading "待查询条件" [level=2] [ref=e29]
          - generic [ref=e30]:
            - generic [ref=e31]:
              - generic [ref=e32]: 时间范围
              - combobox "时间范围" [ref=e33]:
                - option "近七个自然日"
                - option "今天"
                - option "近三十个自然日"
                - option "自定义半开区间" [selected]
                - option "全部历史"
            - generic [ref=e34]:
              - generic [ref=e35]: IANA 时区
              - textbox "IANA 时区" [ref=e36]: Asia/Shanghai
            - generic [ref=e37]:
              - generic [ref=e38]: 开始日期（含）
              - textbox "开始日期（含）" [ref=e39]: 2026-10-05
              - generic [ref=e40]: 自定义期间使用当地日期
            - generic [ref=e41]:
              - generic [ref=e42]: 结束日期（不含）
              - textbox "结束日期（不含）" [ref=e43]: 2026-10-12
              - generic [ref=e44]: 结束日为排他边界
            - generic [ref=e45]:
              - generic [ref=e46]: 会话 ID
              - textbox "会话 ID" [ref=e47]
            - generic [ref=e48]:
              - generic [ref=e49]: 运行用途
              - combobox "运行用途" [ref=e50]:
                - option "全部用途"
                - option "普通聊天" [selected]
                - option "手动聚合"
                - option "模型推理探针"
                - option "记忆提炼"
            - generic [ref=e51]:
              - generic [ref=e52]: 工具身份
              - textbox "工具身份" [ref=e53]:
                - /placeholder: 来源与能力身份
              - generic [ref=e54]: 包含匹配工具的完整 Run
            - generic [ref=e55]:
              - generic [ref=e56]: Run ID
              - textbox "Run ID" [ref=e57]:
                - /placeholder: 精确 Run ID
              - generic [ref=e58]: 使用完整不透明标识
          - status [ref=e59]: 筛选只在点击“刷新账目”时应用。
          - button "刷新账目" [disabled] [ref=e60]
        - status [ref=e61]: 已核验原账目快照；范围与价格保持不变。
        - status [ref=e62]: 连接中断；财务内容为离线快照，暂停读取。
        - region "当前账目快照" [ref=e63]:
          - heading "当前账目快照" [level=2] [ref=e64]
          - paragraph [ref=e65]: 半开期间 [2026-10-04T16:00:00+00:00, 2026-10-11T16:00:00+00:00) · 时区 Asia/Shanghai · Session 全部 · 用途 普通聊天 · 工具身份 全部 · 精确 Run 全部
          - paragraph [ref=e66]: 计算时刻 2026-10-10T16:12:23Z · 有效至 2026-10-10T16:27:23Z
          - group [ref=e67]:
            - generic "快照与价格身份" [ref=e68] [cursor=pointer]
        - paragraph [ref=e69]: 工具筛选展示包含该工具的完整 Run；模型 USD 与工具服务／单位分列，不能归因给单一工具。
        - region "账目汇总" [ref=e70]:
          - heading "分项汇总" [level=2] [ref=e71]
          - paragraph [ref=e72]: Run 55 · 模型 Attempt 110 · 账目覆盖不足 Run 0
          - paragraph [ref=e73]: 精确费用 USD 13.750 · 估算费用 USD 0 · 费用未知 Attempt 0
          - paragraph [ref=e74]: 工具请求 0 · 已确认启动 0 · 明确未启动 0 · 启动未确认 0
          - paragraph [ref=e75]: 精确 Attempt 110 · 估算 Attempt 0 · 使用陈旧价格 0 · 涉及阶梯价 0
          - paragraph [ref=e76]: 估算价格来源：无
          - paragraph [ref=e77]: 工具计量状态：不计费 0 · 已报告计量 0 · 结果未知，请核验原操作 0 · 历史未记录 0
          - group [ref=e78]:
            - generic "Token 用量与缺失覆盖" [ref=e79] [cursor=pointer]
          - paragraph [ref=e80]: 仅统计持久 Run 与可读账目；无 Run 的旧消息不计入。账目覆盖不证明正式回复已保存，完整记录状态请进入运行过程核对。
        - region "Run 账目索引" [ref=e82]:
          - table [ref=e83]:
            - caption [ref=e84]: Run 账目索引
            - rowgroup [ref=e85]:
              - row [ref=e86]:
                - columnheader "Run / Session" [ref=e87]
                - columnheader "用途与状态" [ref=e88]
                - columnheader "归属时间" [ref=e89]
                - columnheader "账目与过程覆盖" [ref=e90]
                - columnheader "操作" [ref=e91]
            - rowgroup [ref=e92]:
              - row [ref=e93]:
                - cell "b807592d4fc84014b8ce7771b3e9cbdb / 4741950f271942c6b61bf7ecf1e523d5" [ref=e94]
                - cell "普通聊天 · finished · completed" [ref=e95]
                - cell "2026-10-10T16:12:22Z（开始时间）" [ref=e96]
                - cell "账目覆盖无已报告缺口 · 未报告过程缺口" [ref=e97]
                - cell [ref=e98]:
                  - button "查看账目 b807592d4fc84014b8ce7771b3e9cbdb" [disabled] [ref=e99]
              - row [ref=e100]:
                - cell "61359214d8bc4ed0b1ea68afd97d203d / f2906d073cea4e8dbcbf52731e0e9d76" [ref=e101]
                - cell "普通聊天 · finished · completed" [ref=e102]
                - cell "2026-10-10T16:12:22Z（开始时间）" [ref=e103]
                - cell "账目覆盖无已报告缺口 · 未报告过程缺口" [ref=e104]
                - cell [ref=e105]:
                  - button "查看账目 61359214d8bc4ed0b1ea68afd97d203d" [disabled] [ref=e106]
              - row [ref=e107]:
                - cell "e177b80cc25846218237a94daa678512 / c442c88ee977429eaa1acb927de67414" [ref=e108]
                - cell "普通聊天 · finished · completed" [ref=e109]
                - cell "2026-10-10T16:12:22Z（开始时间）" [ref=e110]
                - cell "账目覆盖无已报告缺口 · 未报告过程缺口" [ref=e111]
                - cell [ref=e112]:
                  - button "查看账目 e177b80cc25846218237a94daa678512" [disabled] [ref=e113]
              - row [ref=e114]:
                - cell "1daddf97af94460a9e4055edb4ca61a9 / 8e647d4e840f46d7976a8e9e8bd6d5d9" [ref=e115]
                - cell "普通聊天 · finished · completed" [ref=e116]
                - cell "2026-10-10T16:12:22Z（开始时间）" [ref=e117]
                - cell "账目覆盖无已报告缺口 · 未报告过程缺口" [ref=e118]
                - cell [ref=e119]:
                  - button "查看账目 1daddf97af94460a9e4055edb4ca61a9" [disabled] [ref=e120]
              - row [ref=e121]:
                - cell "518ac9ba58d34026934dbe250d039d0c / 3ae7f44d45824c63bec38df3875310c2" [ref=e122]
                - cell "普通聊天 · finished · completed" [ref=e123]
                - cell "2026-10-10T16:12:22Z（开始时间）" [ref=e124]
                - cell "账目覆盖无已报告缺口 · 未报告过程缺口" [ref=e125]
                - cell [ref=e126]:
                  - button "查看账目 518ac9ba58d34026934dbe250d039d0c" [disabled] [ref=e127]
        - region "运行账目明细" [ref=e128]:
          - heading "Run 518ac9ba58d34026934dbe250d039d0c" [level=2] [ref=e129]
          - link "进入运行过程（保留账目快照）" [active] [ref=e130] [cursor=pointer]:
            - /url: /runs/518ac9ba58d34026934dbe250d039d0c?ops_timezone=Asia%2FShanghai&ops_purpose=chat&ops_range=custom&ops_start=2026-10-05&ops_end=2026-10-12&snapshot_id=0d56287ddf7c46629a4f9661153a221f
          - paragraph [ref=e131]: 所属 snapshot_id 0d56287ddf7c46629a4f9661153a221f
          - paragraph [ref=e132]: 未报告过程缺口
          - region "模型 Attempt" [ref=e133]:
            - table [ref=e134]:
              - caption [ref=e135]: 模型 Attempt
              - rowgroup [ref=e136]:
                - row [ref=e137]:
                  - columnheader "Attempt" [ref=e138]
                  - columnheader "模型身份" [ref=e139]
                  - columnheader "结果" [ref=e140]
                  - columnheader "费用" [ref=e141]
                  - columnheader "用量摘要" [ref=e142]
              - rowgroup [ref=e143]:
                - row [ref=e144]:
                  - cell "9311aadc4fb54dd19043b85b925b0283" [ref=e145]
                  - cell "opencode-go / deepseek-v4-flash" [ref=e146]
                  - cell "committed" [ref=e147]
                  - cell "精确费用 USD 0.125" [ref=e148]
                  - cell "输入总量 未报告 · 输出 4" [ref=e149]
                - row [ref=e150]:
                  - cell "f82bf76d81ef47f09f0bb7d3f8b0403a" [ref=e151]
                  - cell "opencode-go / deepseek-v4-flash" [ref=e152]
                  - cell "committed" [ref=e153]
                  - cell "精确费用 USD 0.125" [ref=e154]
                  - cell "输入总量 未报告 · 输出 4" [ref=e155]
          - group [ref=e156]:
            - generic "Token 与四维价格：9311aadc4fb54dd19043b85b925b0283" [ref=e157] [cursor=pointer]
          - group [ref=e158]:
            - generic "Token 与四维价格：f82bf76d81ef47f09f0bb7d3f8b0403a" [ref=e159] [cursor=pointer]
          - paragraph [ref=e160]: 离线副本：保留当前段，重连核验前不可续读。
          - generic "历史正文当前段" [ref=e161]
          - button "下一段" [disabled] [ref=e162]
  - region "MainBar" [ref=e163]:
    - generic [ref=e164]:
      - heading "主对话" [level=2] [ref=e165]
      - button "收起主对话" [ref=e166] [cursor=pointer]
    - complementary "本标签页连接状态" [ref=e167]:
      - paragraph [ref=e168]: 连接已断开；已清除临时文字，正在重新连接。
      - text: 仅影响本标签页
      - button "关闭连接提示" [ref=e169] [cursor=pointer]
    - generic [ref=e170]:
      - generic [ref=e171]: 尚未选择会话
      - button "新建会话" [ref=e172] [cursor=pointer]
    - region "主对话" [ref=e173]
    - generic [ref=e174]:
      - generic [ref=e175]: 消息
      - textbox "消息" [disabled] [ref=e176]:
        - /placeholder: 写下消息；/skills 指定流程…
      - button "发送" [disabled] [ref=e177]
    - paragraph [ref=e178]: 请显式新建或继续会话。
  - status [ref=e179]: 就绪
  - alert [ref=e180]
```

# Test source

```ts
  198 |     const entry=page.getByRole('link',{name:'进入运行过程（保留账目快照）'});
  199 |     const sourceURL=new URL(await entry.getAttribute('href'),server.origin);
  200 |     const snapshot=sourceURL.searchParams.get('snapshot_id');
  201 |     let posts=0; const pages=[];
  202 |     page.on('request',request=>{const url=new URL(request.url());if(url.pathname==='/api/ops/snapshots')posts++;if(url.pathname==='/api/ops')pages.push(url);});
  203 |     await entry.click();
  204 |     await expect(page).toHaveURL(sourceURL.href);
  205 |     await page.goBack();
  206 |     await expect(page.getByRole('region',{name:'当前账目快照'})).toContainText(snapshot);
  207 |     await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(5);
  208 |     await expect(page.getByRole('region',{name:'运行账目明细'})).toContainText(decodeURIComponent(sourceURL.pathname.slice(6)));
  209 |     expect(posts).toBe(0);
  210 |     expect(pages).toHaveLength(1);
  211 |     expect(pages[0].searchParams.get('offset')).toBe('50');
  212 |     expect(pages[0].searchParams.get('snapshot_id')).toBe(snapshot);
  213 |     await expect(entry).toBeFocused();
  214 |     await entry.click();
  215 |     await server.send('expire');
  216 |     await page.goBack();
  217 |     await expect(page.getByRole('status').filter({hasText:'原账目快照已失效'})).toBeVisible();
  218 |     await expect(page.getByLabel('时间范围',{exact:true})).toHaveValue('all');
  219 |     await expect(page.getByLabel('IANA 时区',{exact:true})).toHaveValue('Asia/Shanghai');
  220 |     expect(posts).toBe(0);
  221 |     await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(0);
  222 |     await page.getByRole('button',{name:'刷新账目',exact:true}).click();
  223 |     await expect(page.getByRole('button',{name:/查看账目 /})).toHaveCount(50);
  224 |     expect(posts).toBe(1);
  225 |     await expect(page.getByRole('region',{name:'当前账目快照'})).not.toContainText(snapshot);
  226 |   } finally {await server.close();}
  227 | });
  228 | 
  229 | for(const viewport of [{width:1280,height:800},{width:390,height:844}])
  230 |   test(`S09 migrated Run explicit return restores its visible source at ${viewport.width}px and honors newer intent`,async({page},testInfo)=>{
  231 |   const server=await memoryServer({script:'tests/browser/ops_server.py'});
  232 |   let pending=[];
  233 |   const observations=[];
  234 |   const position=()=>page.evaluate(()=>{
  235 |     const panel=document.querySelector('#page'),link=document.querySelector('a[data-source-key="detail-link"]');
  236 |     const p=panel.getBoundingClientRect(),r=link?.getBoundingClientRect();
  237 |     return {scrollTop:panel.scrollTop,panel:{top:p.top,bottom:p.bottom,height:p.height},link:r?{top:r.top,bottom:r.bottom,height:r.height}:null,
  238 |       anchorOffset:r?r.top-p.top:null,focused:document.activeElement===link,visibleInPanel:!!r&&r.top>=p.top&&r.bottom<=p.bottom};
  239 |   });
  240 |   try{
  241 |     await page.setViewportSize(viewport);
  242 |     await server.send('bulk');
  243 |     const created=page.waitForResponse(response=>response.url().endsWith('/api/ops/snapshots'));
  244 |     await page.goto(server.origin+'/ops?range=7d&timezone=Asia%2FShanghai&purpose=chat');
  245 |     const snapshot=await(await created).json(),rows=page.getByRole('button',{name:/查看账目 /});
  246 |     await expect(rows).toHaveCount(50);
  247 |     await page.getByRole('button',{name:'下一页',exact:true}).click();await expect(rows).toHaveCount(55);
  248 |     await rows.last().click();
  249 |     const entry=page.getByRole('link',{name:'进入运行过程（保留账目快照）',exact:true});
  250 |     const href=await entry.getAttribute('href'),runId=decodeURIComponent(new URL(href,server.origin).pathname.slice(6));
  251 |     let posts=0;const reads=[],origins=[];
  252 |     page.on('request',request=>{const url=new URL(request.url());if(url.pathname==='/api/ops')reads.push(url.href);if(url.pathname==='/api/ops/snapshots')posts++;});
  253 |     for(const intent of ['none','focus','scroll']){
  254 |       await entry.scrollIntoViewIfNeeded();await entry.focus();
  255 |       const beforePosition=await position();expect(beforePosition.visibleInPanel).toBe(true);
  256 |       await entry.click();await expect(page).toHaveURL(server.origin+href);
  257 |       await expect(page.getByRole('region',{name:'运行摘要',exact:true})).toContainText(runId);
  258 |       const origin=await page.evaluate(()=>history.state.alfredShell.source.returnSource);origins.push(origin);
  259 |       expect(origin).toMatchObject({kind:'ops',snapshot_id:snapshot.snapshot_id,process_instance_id:snapshot.process_instance_id,filters:snapshot.filters,offset:50,run_id:runId,trigger:{kind:'detail-link',run_id:runId,href}});
  260 |       const back=page.getByRole('link',{name:'返回来源',exact:true});await expect(back).toHaveAttribute('href',origin.route);
  261 |       const before=reads.length;
  262 |       if(intent!=='none')pending=await holdPages(page,intent==='scroll'?'**/api/ops/detail?**':'**/api/ops?**');
  263 |       await back.click();
  264 |       let intentPosition;
  265 |       if(intent!=='none'){
  266 |         await expect.poll(()=>pending.length).toBe(1);
  267 |         if(intent==='focus')await page.getByLabel('Run ID',{exact:true}).fill('未提交的焦点草稿');
  268 |         else{
  269 |           const panel=page.locator('#page'),rect=await panel.boundingBox();
  270 |           const previous=await panel.evaluate(element=>element.scrollTop);
  271 |           await page.mouse.move(rect.x+rect.width/2,rect.y+rect.height/2);
  272 |           await page.mouse.wheel(0,240);
  273 |           await expect.poll(()=>panel.evaluate(element=>element.scrollTop)).toBeGreaterThan(previous);
  274 |         }
  275 |         intentPosition=await position();pending[0].release();
  276 |       }
  277 |       await expect(rows).toHaveCount(5);
  278 |       await expect(page.getByRole('region',{name:'当前账目快照'})).toContainText(snapshot.snapshot_id);
  279 |       await expect(entry).toHaveAttribute('href',href);
  280 |       await expect(page.getByRole('region',{name:'运行账目明细'})).toContainText(runId);
  281 |       // Await rendering and browser scroll delivery before measuring the actual reading position.
  282 |       await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  283 |       const afterPosition=await position();observations.push({intent,before:beforePosition,intentPosition,after:afterPosition});
  284 |       await writeFile(testInfo.outputPath('source-position-observations.json'),JSON.stringify({viewport,observations},null,2));
  285 |       await page.screenshot({path:testInfo.outputPath(`source-return-${intent}.png`)});
  286 |       expect(reads.slice(before)).toHaveLength(1);
  287 |       const read=new URL(reads[before]);expect(read.searchParams.get('snapshot_id')).toBe(snapshot.snapshot_id);expect(read.searchParams.get('offset')).toBe('50');
  288 |       await expect(page.getByLabel('IANA 时区',{exact:true})).toHaveValue('Asia/Shanghai');
  289 |       await expect(page.getByLabel('运行用途',{exact:true})).toHaveValue('chat');
  290 |       await expect(page.getByLabel('时间范围',{exact:true})).toHaveValue('custom');
  291 |       if(intent==='focus'){
  292 |         await expect(page.getByLabel('Run ID',{exact:true})).toBeFocused();await expect(page.getByLabel('Run ID',{exact:true})).toHaveValue('未提交的焦点草稿');
  293 |         expect(afterPosition.scrollTop).toBeCloseTo(intentPosition.scrollTop,0);
  294 |       }else if(intent==='scroll'){
  295 |         await expect(entry).not.toBeFocused();expect(afterPosition.scrollTop).toBeCloseTo(intentPosition.scrollTop,0);
  296 |       }else{
  297 |         await expect(entry).toBeFocused();expect(afterPosition.visibleInPanel).toBe(true);
> 298 |         expect(afterPosition.anchorOffset).toBeCloseTo(beforePosition.anchorOffset,0);
      |                                            ^ Error: expect(received).toBeCloseTo(expected, precision)
  299 |       }
  300 |       expect(posts).toBe(0);
  301 |       await page.unrouteAll({behavior:'wait'});pending=[];
  302 |     }
  303 |     // Position is per visit; the fixed snapshot/filter/Run identity remains the same.
  304 |     for(const origin of origins)expect(origin).toMatchObject({kind:origins[0].kind,route:origins[0].route,snapshot_id:origins[0].snapshot_id,process_instance_id:origins[0].process_instance_id,filters:origins[0].filters,offset:50,run_id:runId,trigger:origins[0].trigger});
  305 |     await writeFile(testInfo.outputPath('migrated-source-roundtrip.json'),JSON.stringify({viewport,snapshot:snapshot.snapshot_id,filters:snapshot.filters,origins,reads,posts,observations},null,2));
  306 |   }finally{for(const item of pending)item.release();await page.unrouteAll({behavior:'wait'});await server.close();}
  307 | });
  308 | 
  309 | test('S09 direct source reload and read retry never create a replacement snapshot', async ({page,context}) => {
  310 |   const server=await memoryServer({script:'tests/browser/ops_server.py'});
  311 |   let release=()=>{};
  312 |   try {
  313 |     await page.goto(server.origin+'/ops?range=all&timezone=UTC');
  314 |     const identity=await page.getByRole('region',{name:'当前账目快照'}).getByText(/snapshot_id /).textContent();
  315 |     const snapshot=identity.match(/snapshot_id (\S+)/)[1];
  316 |     let posts=0,reads=0;
  317 |     page.on('request',request=>{const path=new URL(request.url()).pathname;if(path==='/api/ops/snapshots')posts++;if(path==='/api/ops')reads++;});
  318 |     await page.route('**/api/ops?**',route=>route.abort('failed'));
  319 |     await page.goto(server.origin+'/ops?range=all&timezone=UTC&snapshot_id='+snapshot);
  320 |     await expect(page.getByText(/原快照核验失败/)).toBeVisible();
  321 |     expect(posts).toBe(0);expect(reads).toBe(1);
  322 |     await page.unroute('**/api/ops?**');
  323 |     await page.getByRole('button',{name:'重新核验原快照',exact:true}).click();
  324 |     await expect(page.getByRole('region',{name:'当前账目快照'})).toContainText(snapshot);
  325 |     expect(posts).toBe(0);expect(reads).toBe(2);
  326 |     await page.reload();
  327 |     await expect(page.getByRole('region',{name:'当前账目快照'})).toContainText(snapshot);
  328 |     expect(posts).toBe(0);expect(reads).toBe(3);
  329 |     let captured;const capturedRead=new Promise(resolve=>{captured=resolve;});const gate=new Promise(resolve=>{release=resolve;});
  330 |     await page.route('**/api/ops?**',async route=>{const response=await route.fetch();captured();await gate;await route.fulfill({response}).catch(error=>{if(!String(error).includes('Route is already handled'))throw error;});});
  331 |     await page.reload();await capturedRead;
  332 |     await context.setOffline(true);await expect(page.getByText(/连接中断；财务内容/)).toBeVisible();
  333 |     release();await page.unroute('**/api/ops?**');await context.setOffline(false);
  334 |     const retry=page.getByRole('button',{name:'重新核验原快照',exact:true});
  335 |     await expect(retry).toBeEnabled();await retry.click();
  336 |     await expect(page.getByRole('region',{name:'当前账目快照'})).toContainText(snapshot);
  337 |     expect(posts).toBe(0);
  338 |   } finally {release();try{await context.setOffline(false);}finally{await server.close();}}
  339 | });
  340 | 
  341 | test('S09 a new process retires a held financial response and clears body without replacing the old ledger',async({page})=>{
  342 |   const server=await memoryServer({script:'tests/browser/ops_migration_server.py'});
  343 |   let release;const gate=new Promise(resolve=>{release=resolve;});
  344 |   try{
  345 |     await server.send('long');
  346 |     await page.goto(server.origin+'/ops?range=all&timezone=UTC');
  347 |     await page.getByRole('button',{name:/查看账目 /}).click();
  348 |     await page.getByRole('button',{name:'完整脱敏审计',exact:true}).click();
  349 |     const preview=page.getByLabel('历史正文当前段');await expect(preview).toContainText('<script>');
  350 |     const scope=page.getByRole('region',{name:'当前账目快照'}),fixed=await scope.textContent();
  351 |     let captured;const persisted=new Promise(resolve=>{captured=resolve;});
  352 |     await page.route('**/api/ops/snapshots',async route=>{const response=await route.fetch();captured();await gate;await route.fulfill({response});});
  353 |     await page.getByRole('button',{name:'刷新账目',exact:true}).click();await persisted;
  354 |     await server.restart();
  355 |     await expect(page.getByText(/旧快照已失效/)).toBeVisible();
  356 |     await expect(preview).toBeEmpty();
  357 |     await expect(page.getByText(/实例已变化；历史正文需重新核验/)).toBeVisible();
  358 |     release();
  359 |     await expect(scope).toHaveText(fixed);
  360 |     await expect(page.getByRole('button',{name:/查看账目 /})).toBeDisabled();
  361 |     await expect(page.getByRole('button',{name:'刷新账目',exact:true})).toBeEnabled();
  362 |     await page.getByRole('button',{name:'核验原操作当前结果',exact:true}).click();
  363 |     await expect(page.getByRole('region',{name:'当前核验',exact:true})).toContainText('读取时刻');
  364 |     await page.getByRole('button',{name:'完整脱敏审计',exact:true}).click();
  365 |     await expect(preview).toContainText('<script>');
  366 |     await expect(scope).toHaveText(fixed);
  367 |   }finally{release();await server.close();}
  368 | });
  369 | 
```