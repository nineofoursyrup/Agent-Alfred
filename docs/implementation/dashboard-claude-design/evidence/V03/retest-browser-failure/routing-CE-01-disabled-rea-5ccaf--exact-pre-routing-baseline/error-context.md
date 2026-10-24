# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: routing.spec.js >> CE-01: disabled real browser conversation matches exact pre-routing baseline
- Location: tests/browser/routing.spec.js:185:1

# Error details

```
Error: expect(received).toBe(expected) // Object.is equality

- Expected  - 1
+ Received  + 3

- /skills off hello
+ /skills off
+ hello

  离线 Skill 回复

  已保存
+ 查看运行
```

# Test source

```ts
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
  202 |         const page = await context.newPage();
  203 |         // Both versions open this real stream only after consuming entry credentials.
  204 |         const ready = page.waitForRequest(request => request.url() === server.origin + '/api/events');
  205 |         await page.goto(server.origin + '/inbox');
  206 |         await ready;
  207 |         await page.getByRole('button', {name:'新建会话', exact:true}).click();
  208 |         await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
  209 |         if(source===baseline)await page.getByRole('button',{name:'展开对话',exact:true}).click();
  210 |         else await page.locator('#shell-toolbar [data-open-panel="mainbar"]').click();
  211 |         await page.getByRole('textbox', {name:'消息'}).fill('/skills off\nhello');
  212 |         await page.getByRole('button', {name:'发送', exact:true}).click();
  213 |         await expect(page.locator('#messages').getByText('离线 Skill 回复')).toBeVisible();
  214 |         await expect(page.getByRole('textbox', {name:'消息'})).toBeEnabled();
  215 |         await page.reload();
  216 |         await expect(page.locator('#messages').getByText('离线 Skill 回复')).toBeVisible();
  217 |         texts.push(await page.locator('#messages').innerText());
  218 |       } finally {
  219 |         try {await context.close();} finally {await server.close();}
  220 |       }
  221 |     }
> 222 |     expect(texts[1]).toBe(texts[0]);
      |                      ^ Error: expect(received).toBe(expected) // Object.is equality
  223 |   } finally {await rm(baseline, {recursive:true, force:true});}
  224 | });
  225 |
  226 | test('S06 CE-13: a delayed save receipt preserves the choice edited after submission',async({page})=>{
  227 |   const server = await memoryServer({script:'tests/browser/routing_server.py'});
  228 |   let release = () => {};
  229 |   try {
  230 |     await page.goto(server.origin+'/behaviour');
  231 |     const choice = page.getByRole('checkbox',{name:'启用消息分流'});
  232 |     await choice.check();
  233 |     let accepted;
  234 |     const started = new Promise(resolve=>accepted=resolve), gate = new Promise(resolve=>release=resolve);
  235 |     await page.route('**/api/behaviour',async route=>{
  236 |       if(route.request().method()!=='POST')return route.continue();
  237 |       const response = await route.fetch();accepted();await gate;await route.fulfill({response});
  238 |     });
  239 |     await page.getByRole('button',{name:'保存设置',exact:true}).click();await started;
  240 |     const draft=page.getByRole('region',{name:'消息分流',exact:true}).locator('.behaviour-draft');
  241 |     await expect(draft).toHaveAttribute('data-dirty','false');
  242 |     await choice.uncheck();
  243 |     await expect(draft).toHaveAttribute('data-dirty','true');
  244 |     await page.getByRole('link',{name:'运行',exact:true}).click();
  245 |     await expect(page.getByRole('dialog')).toBeVisible();
  246 |     await page.getByRole('button',{name:'留在此页',exact:true}).click();
  247 |     await choice.check();
  248 |     await expect(draft).toHaveAttribute('data-dirty','false');
  249 |     await choice.uncheck();
  250 |     await expect(page.getByRole('button',{name:'刷新设置',exact:true})).toBeDisabled();
  251 |     release();
  252 |     await expect(page.getByText('已保存；下一 Run 生效。',{exact:true})).toBeVisible();
  253 |     await expect(choice).not.toBeChecked();
  254 |     await expect(page.getByText(/未提交选择：关闭 · 编辑基线 revision 1/)).toBeVisible();
  255 |     await page.getByRole('button',{name:'刷新设置',exact:true}).click();
  256 |     await expect(page.getByText(/已保存的分流设置：开启 · revision 1/)).toBeVisible();
  257 |     await expect(choice).not.toBeChecked();
  258 |     expect((await(await page.request.get(server.origin+'/api/behaviour')).json()).enabled).toBe(true);
  259 |   } finally {release();await server.close();}
  260 | });
  261 |
  262 | for(const transport of ['delayed','lost']) {
  263 |   test(`S06-ST-01: routing ${transport} receipt does not label submitted-only input as dirty`,async({page})=>{
  264 |     const server=await memoryServer({script:'tests/browser/routing_server.py'});
  265 |     let release=()=>{};
  266 |     try {
  267 |       await page.goto(server.origin+'/behaviour');
  268 |       const choice=page.getByRole('checkbox',{name:'启用消息分流'});await choice.check();
  269 |       let accepted,finished;
  270 |       const admitted=new Promise(resolve=>accepted=resolve),settled=new Promise(resolve=>finished=resolve),gate=new Promise(resolve=>release=resolve);
  271 |       const posts=[];
  272 |       await page.route('**/api/behaviour',async route=>{
  273 |         if(route.request().method()!=='POST')return route.continue();
  274 |         posts.push(route.request().postDataJSON());const response=await route.fetch();accepted({status:response.status(),body:await response.json()});
  275 |         if(transport==='lost')await route.abort();else {await gate;await route.fulfill({response}).catch(()=>{});}
  276 |         finished();
  277 |       });
  278 |       await page.getByRole('button',{name:'保存设置',exact:true}).click();
  279 |       expect(await admitted).toMatchObject({status:200,body:{enabled:true,revision:1}});
  280 |       const region=page.getByRole('region',{name:'消息分流',exact:true}),draft=region.locator('.behaviour-draft');
  281 |       await expect(draft).toHaveAttribute('data-dirty','false');
  282 |       await expect(region.getByText(/已保存的分流设置：关闭 · revision 0/)).toBeVisible();
  283 |       await expect(region.getByText('已保存；下一 Run 生效。',{exact:true})).toHaveCount(0);
  284 |       if(transport==='lost') {
  285 |         await expect(region.getByText(/保存结果未确认，请刷新核验/)).toBeVisible();
  286 |         await choice.uncheck();await expect(draft).toHaveAttribute('data-dirty','true');
  287 |         await region.getByRole('button',{name:'刷新设置',exact:true}).click();
  288 |         await expect(region.getByText(/已保存的分流设置：开启 · revision 1/)).toBeVisible();
  289 |         await expect(choice).not.toBeChecked();
  290 |         await expect(draft).toContainText('未提交选择：关闭 · 编辑基线 revision 0');
  291 |         await expect(region.getByText(/保存结果未确认，请刷新核验/)).toBeVisible();
  292 |         await page.getByRole('link',{name:'运行',exact:true}).click();await expect(page.getByRole('dialog')).toBeVisible();
  293 |         await page.getByRole('button',{name:'留在此页',exact:true}).click();await choice.check();
  294 |         await expect(draft).toHaveAttribute('data-dirty','false');
  295 |       } else await expect(region.getByText('正在保存提交时的选择…',{exact:true})).toBeVisible();
  296 |       await page.getByRole('link',{name:'运行',exact:true}).click();await expect(page).toHaveURL(/\/runs$/);
  297 |       await expect(page.getByRole('dialog')).toHaveCount(0);release();await settled;
  298 |       expect(posts).toEqual([{action:'save',enabled:true,expected_revision:0,fingerprint:null}]);
  299 |       expect(await(await page.request.get(server.origin+'/api/behaviour')).json()).toMatchObject({enabled:true,revision:1});
  300 |     } finally {release();await server.close();}
  301 |   });
  302 | }
  303 |
```
