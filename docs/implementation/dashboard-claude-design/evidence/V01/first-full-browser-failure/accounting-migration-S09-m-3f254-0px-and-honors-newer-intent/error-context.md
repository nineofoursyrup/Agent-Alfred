# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: accounting-migration.spec.js >> S09 migrated Run explicit return restores its visible source at 390px and honors newer intent
- Location: tests/browser/accounting-migration.spec.js:230:3

# Error details

```
Error: expect(received).toBeCloseTo(expected, precision)

Expected: 386.890625
Received: 386.390625

Expected precision:    0
Expected difference: < 0.5
Received difference:   0.5
```

# Page snapshot

```yaml
- generic [ref=e1]:
  - generic [ref=e2]:
    - generic "壳层状态与操作" [ref=e3]:
      - button "打开导航" [ref=e4] [cursor=pointer]
      - button "打开主对话" [ref=e5] [cursor=pointer]
      - region "运行与连接摘要" [ref=e6]:
        - paragraph [ref=e7]: 连接中断或正在同步
    - main [ref=e8]:
      - heading "用量账本" [level=1] [ref=e9]
      - generic [ref=e10]:
        - form "账目筛选" [ref=e11]:
          - heading "待查询条件" [level=2] [ref=e12]
          - generic [ref=e13]:
            - generic [ref=e14]:
              - generic [ref=e15]: 时间范围
              - combobox "时间范围" [ref=e16]:
                - option "近七个自然日"
                - option "今天"
                - option "近三十个自然日"
                - option "自定义半开区间" [selected]
                - option "全部历史"
            - generic [ref=e17]:
              - generic [ref=e18]: IANA 时区
              - textbox "IANA 时区" [ref=e19]: Asia/Shanghai
            - generic [ref=e20]:
              - generic [ref=e21]: 开始日期（含）
              - textbox "开始日期（含）" [ref=e22]: 2026-10-05
              - generic [ref=e23]: 自定义期间使用当地日期
            - generic [ref=e24]:
              - generic [ref=e25]: 结束日期（不含）
              - textbox "结束日期（不含）" [ref=e26]: 2026-10-12
              - generic [ref=e27]: 结束日为排他边界
            - generic [ref=e28]:
              - generic [ref=e29]: 会话 ID
              - textbox "会话 ID" [ref=e30]
            - generic [ref=e31]:
              - generic [ref=e32]: 运行用途
              - combobox "运行用途" [ref=e33]:
                - option "全部用途"
                - option "普通聊天" [selected]
                - option "手动聚合"
                - option "模型推理探针"
                - option "记忆提炼"
            - generic [ref=e34]:
              - generic [ref=e35]: 工具身份
              - textbox "工具身份" [ref=e36]:
                - /placeholder: 来源与能力身份
              - generic [ref=e37]: 包含匹配工具的完整 Run
            - generic [ref=e38]:
              - generic [ref=e39]: Run ID
              - textbox "Run ID" [ref=e40]:
                - /placeholder: 精确 Run ID
              - generic [ref=e41]: 使用完整不透明标识
          - status [ref=e42]: 筛选只在点击“刷新账目”时应用。
          - button "刷新账目" [disabled] [ref=e43]
        - status [ref=e44]: 已核验原账目快照；范围与价格保持不变。
        - status [ref=e45]: 连接中断；财务内容为离线快照，暂停读取。
        - region "当前账目快照" [ref=e46]:
          - heading "当前账目快照" [level=2] [ref=e47]
          - paragraph [ref=e48]: 半开期间 [2026-10-04T16:00:00+00:00, 2026-10-11T16:00:00+00:00) · 时区 Asia/Shanghai · Session 全部 · 用途 普通聊天 · 工具身份 全部 · 精确 Run 全部
          - paragraph [ref=e49]: 计算时刻 2026-10-10T16:12:26Z · 有效至 2026-10-10T16:27:26Z
          - group [ref=e50]:
            - generic "快照与价格身份" [ref=e51] [cursor=pointer]
        - paragraph [ref=e52]: 工具筛选展示包含该工具的完整 Run；模型 USD 与工具服务／单位分列，不能归因给单一工具。
        - region "账目汇总" [ref=e53]:
          - heading "分项汇总" [level=2] [ref=e54]
          - paragraph [ref=e55]: Run 55 · 模型 Attempt 110 · 账目覆盖不足 Run 0
          - paragraph [ref=e56]: 精确费用 USD 13.750 · 估算费用 USD 0 · 费用未知 Attempt 0
          - paragraph [ref=e57]: 工具请求 0 · 已确认启动 0 · 明确未启动 0 · 启动未确认 0
          - paragraph [ref=e58]: 精确 Attempt 110 · 估算 Attempt 0 · 使用陈旧价格 0 · 涉及阶梯价 0
          - paragraph [ref=e59]: 估算价格来源：无
          - paragraph [ref=e60]: 工具计量状态：不计费 0 · 已报告计量 0 · 结果未知，请核验原操作 0 · 历史未记录 0
          - group [ref=e61]:
            - generic "Token 用量与缺失覆盖" [ref=e62] [cursor=pointer]
          - paragraph [ref=e63]: 仅统计持久 Run 与可读账目；无 Run 的旧消息不计入。账目覆盖不证明正式回复已保存，完整记录状态请进入运行过程核对。
        - region "Run 账目索引" [ref=e65]:
          - table [ref=e66]:
            - caption [ref=e67]: Run 账目索引
            - rowgroup [ref=e68]:
              - row [ref=e69]:
                - columnheader "Run / Session" [ref=e70]
                - columnheader "用途与状态" [ref=e71]
                - columnheader "归属时间" [ref=e72]
                - columnheader "账目与过程覆盖" [ref=e73]
                - columnheader "操作" [ref=e74]
            - rowgroup [ref=e75]:
              - row [ref=e76]:
                - cell "d9608cf66c1d4d7d95ba3f83cdcc5516 / b5b0ceaa1fd54907aac827657d3bdcbe" [ref=e77]
                - cell "普通聊天 · finished · completed" [ref=e78]
                - cell "2026-10-10T16:12:25Z（开始时间）" [ref=e79]
                - cell "账目覆盖无已报告缺口 · 未报告过程缺口" [ref=e80]
                - cell [ref=e81]:
                  - button "查看账目 d9608cf66c1d4d7d95ba3f83cdcc5516" [disabled] [ref=e82]
              - row [ref=e83]:
                - cell "199d9320ac8a48b38b79c5462475d4f0 / 2af60a5df82648e3a960c3c5b978b73f" [ref=e84]
                - cell "普通聊天 · finished · completed" [ref=e85]
                - cell "2026-10-10T16:12:25Z（开始时间）" [ref=e86]
                - cell "账目覆盖无已报告缺口 · 未报告过程缺口" [ref=e87]
                - cell [ref=e88]:
                  - button "查看账目 199d9320ac8a48b38b79c5462475d4f0" [disabled] [ref=e89]
              - row [ref=e90]:
                - cell "d9861b796e6541948152a60cc2a87877 / 387ffd181ba64d59a9c01015f8a8f2fc" [ref=e91]
                - cell "普通聊天 · finished · completed" [ref=e92]
                - cell "2026-10-10T16:12:25Z（开始时间）" [ref=e93]
                - cell "账目覆盖无已报告缺口 · 未报告过程缺口" [ref=e94]
                - cell [ref=e95]:
                  - button "查看账目 d9861b796e6541948152a60cc2a87877" [disabled] [ref=e96]
              - row [ref=e97]:
                - cell "6ea4fe8a7a504aa5aed64d2e2f9d4ba5 / e41d5e779e9240fc893b584ff62685b9" [ref=e98]
                - cell "普通聊天 · finished · completed" [ref=e99]
                - cell "2026-10-10T16:12:25Z（开始时间）" [ref=e100]
                - cell "账目覆盖无已报告缺口 · 未报告过程缺口" [ref=e101]
                - cell [ref=e102]:
                  - button "查看账目 6ea4fe8a7a504aa5aed64d2e2f9d4ba5" [disabled] [ref=e103]
              - row [ref=e104]:
                - cell "af095faa872f4709bb578f663c99c0db / 98318088c960471ca5e6b84e8122227b" [ref=e105]
                - cell "普通聊天 · finished · completed" [ref=e106]
                - cell "2026-10-10T16:12:25Z（开始时间）" [ref=e107]
                - cell "账目覆盖无已报告缺口 · 未报告过程缺口" [ref=e108]
                - cell [ref=e109]:
                  - button "查看账目 af095faa872f4709bb578f663c99c0db" [disabled] [ref=e110]
        - region "运行账目明细" [ref=e111]:
          - heading "Run af095faa872f4709bb578f663c99c0db" [level=2] [ref=e112]
          - link "进入运行过程（保留账目快照）" [active] [ref=e113] [cursor=pointer]:
            - /url: /runs/af095faa872f4709bb578f663c99c0db?ops_timezone=Asia%2FShanghai&ops_purpose=chat&ops_range=custom&ops_start=2026-10-05&ops_end=2026-10-12&snapshot_id=cf798729b05b4ae5a7a86fa21f8259cb
          - paragraph [ref=e114]: 所属 snapshot_id cf798729b05b4ae5a7a86fa21f8259cb
          - paragraph [ref=e115]: 未报告过程缺口
          - region "模型 Attempt" [ref=e116]:
            - table [ref=e117]:
              - caption [ref=e118]: 模型 Attempt
              - rowgroup [ref=e119]:
                - row [ref=e120]:
                  - columnheader "Attempt" [ref=e121]
                  - columnheader "模型身份" [ref=e122]
                  - columnheader "结果" [ref=e123]
                  - columnheader "费用" [ref=e124]
                  - columnheader "用量摘要" [ref=e125]
              - rowgroup [ref=e126]:
                - row [ref=e127]:
                  - cell "1502a5df7e964cd898b27b4a2ff94ee8" [ref=e128]
                  - cell "opencode-go / deepseek-v4-flash" [ref=e129]
                  - cell "committed" [ref=e130]
                  - cell "精确费用 USD 0.125" [ref=e131]
                  - cell "输入总量 未报告 · 输出 4" [ref=e132]
                - row [ref=e133]:
                  - cell "649f2731479d42658a4b0c6cf4cf2d15" [ref=e134]
                  - cell "opencode-go / deepseek-v4-flash" [ref=e135]
                  - cell "committed" [ref=e136]
                  - cell "精确费用 USD 0.125" [ref=e137]
                  - cell "输入总量 未报告 · 输出 4" [ref=e138]
          - group [ref=e139]:
            - generic "Token 与四维价格：1502a5df7e964cd898b27b4a2ff94ee8" [ref=e140] [cursor=pointer]
          - group [ref=e141]:
            - generic "Token 与四维价格：649f2731479d42658a4b0c6cf4cf2d15" [ref=e142] [cursor=pointer]
          - paragraph [ref=e143]: 离线副本：保留当前段，重连核验前不可续读。
          - generic "历史正文当前段" [ref=e144]
          - button "下一段" [disabled] [ref=e145]
  - status [ref=e146]: 就绪
  - alert [ref=e147]
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