// Run after installing old/new/supported packages into separate environments.
// Manifest supplies exact Python/artifact identities; one retained browser tab
// spans sequential normal closes on one previously absent isolated state path.
import {chromium,expect} from '@playwright/test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {createInterface} from 'node:readline';
import {readFile,writeFile,mkdir,copyFile,stat} from 'node:fs/promises';
import {join,resolve} from 'node:path';
import {createHash} from 'node:crypto';
const manifest=JSON.parse(await readFile(process.argv[2],'utf8'));
const directory=resolve(manifest.directory),state=join(directory,'state');
await mkdir(directory,{recursive:true});
await assert.rejects(stat(state),{code:'ENOENT'});
await copyFile(new URL('./dashboard_lifecycle_server.py',import.meta.url),join(directory,'server.py'));
const origin=`http://127.0.0.1:${manifest.port}`,proof={manifest,stages:[],requests:[]};
const browser=await chromium.launch();
const context=await browser.newContext({viewport:{width:1440,height:900}});
const page=await context.newPage();let live,token,stage;
page.on('request',r=>{if(r.method()==='POST')proof.requests.push({stage,path:new URL(r.url()).pathname,body:r.postDataJSON()});});
async function start(label){
  stage=label;const candidate=manifest[label];
  const env={...process.env,PYTHON_DOTENV_DISABLED:'1'};delete env.PYTHONPATH;delete env.PYTHONHOME;
  const child=spawn(candidate.python,['-B',join(directory,'server.py'),'--state',state,'--port',String(manifest.port)],{cwd:directory,env});
  const done=once(child,'close'),events=[];let stderr='';child.stderr.on('data',d=>stderr+=d);
  const lines=createInterface({input:child.stdout});let ready;
  const initialized=new Promise(resolve=>ready=resolve);
  lines.on('line',line=>{const event=JSON.parse(line);events.push(event);if(event.event==='ready')ready(event);});
  const meta=await Promise.race([initialized,done.then(()=>{throw new Error(stderr);})]);
  live={child,done,lines,events,stderr:()=>stderr};
  const entry=await get('/api/entry');token=entry.csrf_token;
  assert.equal(entry.instance_id,meta.instance);
  if(proof.stages.length)assert.notEqual(meta.instance,proof.stages.at(-1).ready.instance);
  const html=await(await page.request.get(origin+'/')).body();
  assert.equal(createHash('sha256').update(html).digest('hex'),meta.html_sha256);
  proof.stages.push({label,ready:meta,entry,artifact:candidate});
}
async function stop(){
  if(!live)return;
  live.child.stdin.end('stop\n');const [code,signal]=await live.done;
  proof.stages.at(-1).close={code,signal,events:live.events,stderr:live.stderr()};
  assert.equal(code,0);assert.equal(signal,null);assert.equal(live.events.at(-1).closed,true);
  live.lines.close();live=null;
}
async function get(path,status=200){const r=await page.request.get(origin+path);assert.equal(r.status(),status,path);return r.json();}
async function post(path,body,status=200){const r=await page.request.post(origin+path,{headers:{'x-agent-alfred-csrf':token},data:body});assert.equal(r.status(),status,await r.text());return r.json();}
async function settings(op,fields){const before=await get('/api/models');return post('/api/settings',{op,expected_revision:before.revision,endpoint_id:'opencode-go',model_id:'deepseek-v4-flash',...fields});}
async function memory(operation_id,subject,fact){return post('/api/memory/commands',{schema_version:1,operation_id,kind:'semantic',action:'save',payload:{subject,fact}});}
async function send(text){
  await page.getByRole('textbox',{name:'消息',exact:true}).fill(text);
  const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/runs'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'发送',exact:true}).click();const r=await response;assert.equal(r.status(),202);const identity=await r.json();
  await expect.poll(async()=>{const v=await get('/api/runs?filter=all');return v.runs.some(x=>x.run_id===identity.run_id);}).toBe(true);
  await expect.poll(async()=>JSON.stringify(await get('/api/sessions/messages?'+new URLSearchParams({session_id:identity.session_id,page_size:'100'})))).toContain(text);
  return identity;
}
try{
  await start('old');await page.goto(origin+'/inbox');
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  const oldRun=await send('旧包的持久消息');const session=oldRun.session_id;
  const retained=await memory('old-retained','旧包保留记忆','仍需保留的旧事实');
  const forgotten=await memory('old-forget','需要遗忘的旧记忆','不得在回退后复活');
  await settings('display',{display_name:'旧包显示名'});await settings('price',{dimension:'output',value:'0'});
  const oldAccounting=await post('/api/ops/snapshots',{range:'all',timezone:'UTC'});
  const oldDetail=await get('/api/ops/detail?'+new URLSearchParams({snapshot_id:oldAccounting.snapshot_id,run_id:oldRun.run_id}));
  await page.getByRole('textbox',{name:'消息',exact:true}).fill('跨整包切换保留草稿');
  const deepLink=`/runs/${encodeURIComponent(oldRun.run_id)}?filter=all`;
  await page.goto(origin+deepLink);await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveValue('跨整包切换保留草稿');
  proof.old={oldRun,session,retained,forgotten,oldAccounting,oldDetail,deepLink};await stop();
  await start('current');const beforeWrites=proof.requests.length;await page.reload();
  await expect(page.getByRole('heading',{name:'运行',exact:true})).toBeVisible();
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveValue('跨整包切换保留草稿');
  assert.equal(await page.evaluate(()=>sessionStorage.getItem('alfred.session')),session);
  assert.equal(page.url(),origin+deepLink);assert.equal(proof.requests.length,beforeWrites);
  const overview=await context.newPage();await overview.goto(origin+'/');await expect(overview).toHaveURL(origin+'/overview');await overview.close();
  assert.equal((await get('/api/memory/record?kind=semantic&id='+retained.result.memory_id)).record.fact,'仍需保留的旧事实');
  await settings('display',{display_name:null});await settings('price',{dimension:'output',value:null});
  const nextRun=await send('新包的持久消息');assert.equal(nextRun.session_id,session);
  const newMemory=await memory('new-retained','新包保留记忆','回退仍应读取的新事实');
  const deletion=await post('/api/memory/commands',{schema_version:1,operation_id:'new-forget',kind:'semantic',action:'delete',payload:{id:forgotten.result.memory_id},expected_version:1});
  assert.equal(deletion.result.status,'deleted');
  await expect.poll(async()=>(await get('/api/memory/operations?operation_id=new-forget')).forgetting?.state).toBe('complete');
  const beforeRollback=await get('/api/models');
  const currentAccounting=await post('/api/ops/snapshots',{range:'all',timezone:'UTC'});
  const currentOldDetail=await get('/api/ops/detail?'+new URLSearchParams({snapshot_id:currentAccounting.snapshot_id,run_id:oldRun.run_id}));
  const cat=await get('/api/database'),query=await post('/api/database/queries',{});
  const queryResult=await post(`/api/database/queries/${query.query_id}/execute`,{instance_id:cat.instance_id,memory_revision:cat.memory_revision,protection_version:cat.protection_version,sql:'SELECT count(*) AS n FROM diag_sessions'});
  assert.equal(queryResult.rows[0][0].value,'1');
  await page.getByRole('textbox',{name:'消息',exact:true}).fill('支持回退仍保留草稿');
  proof.current={nextRun,newMemory,deletion,beforeRollback,currentAccounting,currentOldDetail,query,queryResult,automaticPostsOnRefresh:proof.requests.length-beforeWrites-1};await stop();
  await start('supported');const rollbackWrites=proof.requests.length;await page.reload();
  await expect(page.getByRole('heading',{name:'运行',exact:true})).toBeVisible();
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveValue('支持回退仍保留草稿');
  assert.equal(await page.evaluate(()=>sessionStorage.getItem('alfred.session')),session);
  assert.equal(page.url(),origin+deepLink);assert.equal(proof.requests.length,rollbackWrites);
  const modelState=await get('/api/models'),saved=modelState.endpoints.flatMap(g=>g.models).find(m=>m.endpoint_id==='opencode-go'&&m.model_id==='deepseek-v4-flash');
  assert.equal(saved.display_name_override,null);assert.equal(saved.price_override,null);assert.equal(modelState.revision,beforeRollback.revision);
  const history=await get('/api/sessions/messages?'+new URLSearchParams({session_id:session,page_size:'100'}));
  const serialized=JSON.stringify(history);assert(serialized.includes('旧包的持久消息'));assert(serialized.includes('新包的持久消息'));
  const runs=await get('/api/runs?filter=all');assert.deepEqual(new Set(runs.runs.map(r=>r.run_id)),new Set([oldRun.run_id,nextRun.run_id]));
  assert.equal((await get('/api/memory/record?kind=semantic&id='+retained.result.memory_id)).record.fact,'仍需保留的旧事实');
  assert.equal((await get('/api/memory/record?kind=semantic&id='+newMemory.result.memory_id)).record.fact,'回退仍应读取的新事实');
  const absent=await get('/api/memory/record?kind=semantic&id='+forgotten.result.memory_id,404);
  const receipt=await get('/api/memory/operations?operation_id=new-forget');assert.equal(receipt.forgetting.state,'complete');
  const accounting=await post('/api/ops/snapshots',{range:'all',timezone:'UTC'});assert.deepEqual(accounting.summary,currentAccounting.summary);
  const detail=await get('/api/ops/detail?'+new URLSearchParams({snapshot_id:accounting.snapshot_id,run_id:oldRun.run_id}));
  proof.supported={modelState,history,runs,absent,receipt,accounting,detail,automaticPostsOnRefresh:proof.requests.length-rollbackWrites};
  const fallback=await context.newPage();await fallback.goto(origin+'/');await expect(fallback.getByRole('heading',{name:'收件箱',exact:true})).toBeVisible();await expect(fallback.locator('.brand')).toHaveAttribute('href','/inbox');await fallback.close();
  proof.lostCapabilities=['默认根地址与品牌入口回到收件箱；已迁移十页和全部语义修复仍可用'];
  await stop();proof.status='PASS';
}catch(error){proof.status='FAIL';proof.error=String(error.stack||error);throw error;}
finally{try{await stop();}finally{await context.close();await browser.close();await writeFile(join(directory,'result.json'),JSON.stringify(proof,null,2)+'\n');}}
