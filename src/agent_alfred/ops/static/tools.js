import { node } from './dom.js';
/** @typedef {Record<string, any>} Wire */

/** @param {string} path @param {Wire|undefined} body @param {string} csrf @param {AbortSignal} [signal] */
export async function request(path, body, csrf, signal) {
  const response = await fetch(path, {method: body ? 'POST' : 'GET', signal,
    cache: 'no-store', headers: body ? {'Content-Type': 'application/json', 'x-agent-alfred-csrf': csrf} : {},
    ...(body ? {body: JSON.stringify(body)} : {})});
  const value = await response.json();
  return {ok: response.ok, value, status: response.status};
}
/** @param {Wire} tool */
function sourceGroup(tool) {
  const source = tool.source_id;
  if (source === 'builtin') return {key:'builtin', name:'内置工具', order:0, source};
  if (typeof source === 'string' && source.startsWith('mcp:') && tool.server_key)
    return {key:JSON.stringify(['mcp',tool.server_key,source]), name:`MCP · ${tool.server_key}`, order:2, source};
  if (tool.observation && tool.integration_revision !== undefined)
    return {key:JSON.stringify(['integration',source]), name:`集成 · ${source}`, order:1, source};
  return {key:JSON.stringify(['unknown',source]), name:`未知来源 · ${source || '未提供'}`, order:3, source};
}
/** @param {string} a @param {string} b */
function compare(a,b) { return a.localeCompare(b,'zh-CN') || (a < b ? -1 : a > b ? 1 : 0); }
/** Retain existing nodes when their stable order has not changed. @param {HTMLElement} parent @param {HTMLElement[]} children */
function arrange(parent, children) {
  const keep = new Set(children);
  for (const child of [...parent.children]) if (!keep.has(/** @type {HTMLElement} */(child))) child.remove();
  children.forEach((child,index)=>{if(parent.children[index]!==child) parent.insertBefore(child,parent.children[index]||null);});
}
/** @param {HTMLElement} root @param {()=>string} csrf */
export function toolsPage(root, csrf) {
  root.classList.add('tools-page');
  const mcpState=node('p');mcpState.className='tool-attention';
  const state = node('p'), readState = node('p','读取工具目录…'), operation = node('p');
  readState.setAttribute('role','status'); operation.setAttribute('role','status');
  state.className = 'tools-configuration'; operation.className = 'tool-attention';
  const controls = node('div'); controls.className = 'tools-controls';
  const refresh = node('button','核对当前授权'), reapply = node('button','重新应用已保存授权');
  const connection = node('a','连接、凭据与 MCP 维护'); connection.href='/connections';
  controls.append(refresh,reapply,connection);
  const list = node('div'), history = node('section');
  history.setAttribute('aria-label','历史不可调用目录');
  const historyTitle=node('h2','历史不可调用目录'), historicalList=node('div');
  history.append(historyTitle,node('p','保留原来源身份供查看调用记录；历史工具不可调用，旧草稿不转移。'),historicalList);
  root.append(state,readState,operation,mcpState,controls,node('p','从 MainBar 明确提出工具请求；本页查看来源、能力和授权。人格 read_persona / update_persona 保留完整版本、覆盖限制与冲突，修改从下一 Run 生效。'),list,history);
  let generation=0, alive=true, busy=false, online=true, verified=false, expectedInstance='', detailNumber=0;
  /** @type {Wire|null} */ let current=null;
  /** @type {Wire|null} */ let submission=null;
  /** @type {Map<string,{value:string, revision:number, instance:string}>} */ const drafts=new Map();
  /** @type {Map<string,string>} */ const notices=new Map();
  /** @type {Map<string,Wire>} */ const retired=new Map();
  /** @type {Map<string,ReturnType<typeof createRow>>} */ const rows=new Map();
  /** @type {Map<string,{root:HTMLElement, body:HTMLElement, status:HTMLElement}>} */ const groups=new Map();
  const controller=new AbortController();
  /** @param {Wire} tool */
  const saved = tool => current?.authorizations?.[tool.identity] || 'unset';
  /** @param {{revision:number,instance:string}} draft */
  const conflict = draft => draft.revision!==current?.revision || draft.instance!==current?.process_instance_id;
  const canWrite = () => online && verified && !busy && current?.configuration_state==='ok' && !current?.external_change;
  /** @param {Wire} tool @param {boolean} historical */
  function createRow(tool,historical) {
    const card=node('article'); card.className='tool-row'; card.dataset.toolIdentity=tool.identity||'';
    const title=node('h3',tool.name), origin=node('p'), brief=node('p'), status=node('div'), feedback=node('p');
    origin.className='tool-identity'; brief.className='tool-brief'; feedback.className='tool-attention';
    status.className='tool-status'; status.setAttribute('aria-label','工具状态摘要');
    const availability=node('p'), connectionState=node('p'), authorization=node('p'), exposure=node('p'), reason=node('p'), draftState=node('p');
    status.append(availability,connectionState,authorization,exposure,reason,draftState);
    const actions=node('div'); actions.className='tools-controls';
    const toggle=node('button','展开详情'), detail=node('section');
    detail.id=`tool-detail-${++detailNumber}`; detail.hidden=true; detail.className='tool-detail';
    toggle.setAttribute('aria-controls',detail.id); toggle.setAttribute('aria-expanded','false');
    toggle.onclick=()=>{const open=detail.hidden;if(!open&&detail.contains(document.activeElement))toggle.focus({preventScroll:true});detail.hidden=!open;toggle.textContent=open?'收起详情':'展开详情';toggle.setAttribute('aria-expanded',String(open));};
    const calls=node('a','查看包含该工具的运行');
    if(tool.identity) calls.href='/ops?tool='+encodeURIComponent(tool.identity);
    else calls.textContent='历史完整身份无法确认，不能定位调用';
    const maintenance=node('a','连接维护'); maintenance.href='/connections';
    actions.append(toggle,calls);
    if(tool.effect==='external'||tool.server_key)actions.append(maintenance);
    const identity=node('p'), description=node('p'), provenance=node('p'), observation=node('p'), stored=node('p'), disk=node('p'), baseline=node('p');
    identity.className='tool-identity'; description.className='tool-description';
    detail.append(identity,description,provenance,observation,stored,disk,baseline);
    const select=node('select'), label=node('label','授权草稿'), save=node('button','保存授权'), accept=node('button','基于当前版本继续编辑');
    if(tool.effect==='external'&&!historical) {
      select.setAttribute('aria-label',`${tool.name} 授权草稿`);
      for(const [key,text] of [['unset','未决定'],['allowed','允许'],['denied','拒绝']]){const option=node('option',text);option.value=key;select.append(option);}
      label.append(select);detail.append(label,save,accept);
      select.onchange=()=>{
        if(!current)return;
        const old=drafts.get(tool.identity);
        // Pending submissions and unverified saved values cannot prove no edit.
        // Keep new intent with its original CAS baseline until explicit comparison.
        if(verified&&online&&submission?.identity!==tool.identity&&select.value===saved(tool)&&(!old||!conflict(old)))drafts.delete(tool.identity);
        else drafts.set(tool.identity,{value:select.value,revision:old?.revision??current.revision,instance:old?.instance??current.process_instance_id});
        render();
      };
      save.onclick=()=>{
        if(!canWrite()||!current)return;
        if(!drafts.has(tool.identity))drafts.set(tool.identity,{value:select.value,revision:current.revision,instance:current.process_instance_id});
        const draft=drafts.get(tool.identity);
        if(draft?.instance!==current.process_instance_id){notices.set(tool.identity,'服务实例已变化；请比较后显式基于当前版本继续编辑。');render();return;}
        void mutate('/api/tools/authorization',{identity:tool.identity,authorization:select.value,expected_revision:draft?.revision});
      };
      accept.onclick=()=>{
        if(!current||!verified||!online)return;
        drafts.set(tool.identity,{value:select.value,revision:current.revision,instance:current.process_instance_id});render();
      };
    }
    card.append(title,origin,brief,status,feedback,actions,detail);
    /** @param {Wire} item */
    function update(item) {
      const external=item.effect==='external', draft=drafts.get(item.identity);
      title.textContent=item.name;
      origin.textContent=`来源 ${item.source_id||'未知'} · 能力 ${item.capability_id||item.name||'未知'}`;
      brief.textContent=item.description||'未提供描述';
      availability.textContent=`副作用：${item.effect||'未知'} · 可用性：${historical?'historical_directory':item.availability||'未知'}`;
      connectionState.textContent=external?`${historical?'历史连接观测':'连接观测'}：${verified&&online?item.connection||'未知':'待核验（旧观测 '+(item.connection||'未知')+'）'}`:'本地工具，不需要外部授权';
      authorization.textContent=external?`已保存授权：${saved(item)} · 实际应用：${historical?'不可调用':verified&&online?(current?.application_state||'未知'):'未知，待核验'}`:'授权：不适用';
      exposure.textContent=`模型暴露：${historical?'hidden':external&&(!verified||!online)?'unverified':item.exposure||'未知'}`;
      reason.textContent=`阻止原因：${historical?(item.reason||'已不在当前目录'):external&&(!verified||!online)?'当前授权状态无法核实':item.reason||'无已报告阻止原因'}`;
      draftState.textContent=draft?`未保存草稿：${draft.value} · 基线版本 ${draft.revision}${conflict(draft)?' · 版本冲突，需显式比较确认':''}`:'';
      draftState.hidden=!draft;
      feedback.textContent=notices.get(item.identity)||item.retirement_notice||'';feedback.hidden=!feedback.textContent;
      identity.textContent=`完整能力身份：${item.identity||'未知'}；完整来源：${item.source_id||'未知'}；能力：${item.capability_id||item.name||'未知'}`;
      description.textContent=item.description||'未提供描述';
      provenance.textContent=item.server_key?`MCP 服务器 ${item.server_key} · 原始工具名 ${item.original_name||'未知'} · 别名 ${item.alias||'历史未提供'}`:'来源以注册身份为准，不按名称或描述推断。';
      const observed=item.observation;
      observation.textContent=observed?`连接观测时间：${observed.checked_at||'未观测'} · 方式：${observed.checked_via||'未知'} · 原因：${observed.reason||'无已报告原因'}`:'连接观测：未提供独立时间／方式；不推断认证成功。';
      stored.textContent=external?`服务端当前保存值：${saved(item)} · 保存版本 ${current?.revision??'未知'}`:'本地工具，不需要外部授权';
      disk.textContent=current?.disk_configuration?`磁盘外部版本 ${current.disk_configuration.revision}：${current.disk_configuration.authorizations?.[item.identity]||'unset'}；保留当前运行时配置，请核对文件并重启加载。`:'';
      disk.hidden=!disk.textContent;
      baseline.textContent=draft?`草稿基于版本 ${draft.revision}；当前版本 ${current?.revision}，${conflict(draft)?'请比较后确认。':'仅显式保存才提交。'} 基线实例 ${draft.instance}`:'';
      baseline.hidden=!draft;
      if(external&&!historical){
        select.setAttribute('aria-label',`${item.name} 授权草稿`);select.value=draft?.value||saved(item);
        save.disabled=!canWrite();accept.hidden=!draft||!conflict(draft);accept.disabled=!verified||!online||busy;
      }
    }
    return {root:card,update};
  }
  function render() {
    if(!current||!alive)return;
    const value=current;
    state.textContent=`配置：${value.configuration_state} · 保存版本 ${value.revision} · 生效：${verified&&online?value.application_state:'未知，待核验'}${value.external_change?' · 检测到磁盘变化，禁止覆盖':''}${value.active_policy_retained?' · 在途操作保留原政策；当前外部授权待核验，空闲后暂停外部能力。':''}`;
    reapply.disabled=!canWrite();
    mcpState.textContent=`MCP 目录：${value.mcp?.error || ((value.mcp?.servers||[]).length?'按各服务器观测分别核对':'未配置服务器')}${verified&&online?'':'（旧观察，待核验）'}`;
    /** @type {Map<string,Wire>} */const catalog=new Map();
    for(const tool of value.tools){
      const group=sourceGroup(tool);
      if(!catalog.has(group.key))catalog.set(group.key,{...group,tools:[]});
      catalog.get(group.key)?.tools.push(tool);
    }
    // A configured server without a current tool still has observable connection facts.
    for(const server of value.mcp?.servers||[]){
      if(![...catalog.values()].some(group=>group.tools.some(/** @param {Wire} tool */tool=>tool.server_key===server.server_key)))
        catalog.set(JSON.stringify(['mcp-empty',server.server_key]),{key:JSON.stringify(['mcp-empty',server.server_key]),name:`MCP · ${server.server_key}`,source:'当前来源身份未确认',order:2,tools:[],server});
    }
    const activeKeys=new Set(), groupNodes=[];
    for(const group of [...catalog.values()].sort((a,b)=>a.order-b.order||compare(a.name,b.name)||compare(a.key,b.key))){
      let view=groups.get(group.key);
      if(!view){const section=node('section'),heading=node('h2',group.name),source=node('p',group.source),summary=node('p'),body=node('div');section.className='tools-source';section.setAttribute('aria-label',group.order===2?`${group.name} · ${group.source}`:group.name);source.className='tool-identity';section.append(heading,source,summary,body);view={root:section,body,status:summary};groups.set(group.key,view);}
      const server=group.server||(value.mcp?.servers||[]).find(/** @param {Wire} item */item=>item.server_key===group.tools[0]?.server_key);
      view.status.textContent=server?`连接：${verified&&online?server.state:'待核验'} · 启动许可：${server.enabled?'已允许':'未允许'} · 可用工具 ${server.available_tools??'未知'} / ${server.total_tools??'未知'}（不是已授权数） · ${server.reason||'无已报告原因'}${server.cleanup_incomplete?' · 清理未完成':''}${server.directory_changed?' · 目录可能变化':''} · 观测 ${server.observed_at||'未知'}`:'';
      const cards=[];
      for(const tool of group.tools.sort(/** @param {Wire} a @param {Wire} b */(a,b)=>compare(a.name,b.name)||compare(a.identity,b.identity))){const key='current:'+tool.identity;activeKeys.add(key);let row=rows.get(key);if(!row){row=createRow(tool,false);rows.set(key,row);}row.update(tool);cards.push(row.root);}
      arrange(view.body,cards);groupNodes.push(view.root);
    }
    arrange(list,groupNodes);
    const historicalCards=[];
    for(const tool of [...retired.values()].sort((a,b)=>compare(a.server_key||a.source_id,b.server_key||b.source_id)||compare(a.name,b.name)||compare(a.identity||'',b.identity||''))){
      const key='history:'+(tool.identity||JSON.stringify([tool.source_id,tool.server_key,tool.name]));activeKeys.add(key);let row=rows.get(key);if(!row){row=createRow(tool,true);rows.set(key,row);}row.update(tool);historicalCards.push(row.root);
    }
    arrange(historicalList,historicalCards);history.hidden=historicalCards.length===0;
    for(const [key,row] of rows)if(!activeKeys.has(key)){row.root.remove();rows.delete(key);}
    for(const [key,group] of groups)if(!catalog.has(key)){group.root.remove();groups.delete(key);}
  }
  /** @param {Wire} value */
  function acceptCatalog(value) {
    const live=new Set(value.tools.map(/** @param {Wire} tool */tool=>tool.identity));
    for(const tool of current?.tools||[])if(!live.has(tool.identity)){
      const discarded=drafts.delete(tool.identity);
      const priorReceipt=notices.get(tool.identity);
      notices.delete(tool.identity);
      retired.set(tool.identity,{...tool,historical:true,reason:'已不在当前目录，来源已消失或被替换',retirement_notice:(discarded?'原身份授权草稿已失效并清除；不转移到同名新工具。':'本页先前观察的目录，不能证明当前仍可调用。')+(priorReceipt?' 历史操作回执：'+priorReceipt:'')});
    }
    for(const item of value.mcp_history||[]){
      const identity=typeof item.source_id==='string'&&typeof item.capability_id==='string'?JSON.stringify([item.source_id,item.capability_id]):null;
      if(!live.has(identity))retired.set(identity||JSON.stringify([item.source_id,item.server_key,item.name]),{...item,identity});
    }
    for(const identity of live)retired.delete(identity);
    current=value;verified=true;readState.textContent='当前目录已核对；连接观测、保存与生效分别显示。';render();
  }
  async function load() {
    if(!online||!alive)return;
    const mine=++generation;
    readState.textContent='正在核对当前工具与授权…';
    try{
      const result=await request('/api/tools',undefined,csrf(),controller.signal);
      if(!alive||!online||mine!==generation)return;
      const value=result.value;
      if(!result.ok||!Array.isArray(value.tools)||typeof value.process_instance_id!=='string'||!Number.isInteger(value.revision)||!value.authorizations)throw new Error();
      if(expectedInstance&&expectedInstance!==value.process_instance_id)throw new Error();
      acceptCatalog(value);return value;
    }catch{if(alive&&mine===generation){verified=false;readState.textContent='当前授权状态无法核实；保留旧目录与草稿，外部生效状态待核验。';render();}}
  }
  /** @param {string} path @param {Wire} body */
  async function mutate(path,body) {
    if(!canWrite()||!current)return;
    busy=true;++generation;
    const instance=current.process_instance_id, submitted=drafts.get(body.identity);
    submission={identity:body.identity,draft:submitted};
    let notice='提交结果尚未确认，仅核对当前值；不会自动重送。';
    /** @type {Wire|null} */let receipt=null;
    operation.textContent='正在提交；之后的新草稿不会改变本次请求。';render();
    try{
      const result=await request(path,body,csrf(),controller.signal);
      if(!alive)return;
      if(result.ok&&Array.isArray(result.value.tools)&&!result.value.error&&(!body.identity||result.value.saved===true)){
        receipt=result.value;notice=`服务端收到版本 ${result.value.revision} 操作回执；当前状态以回读为准。`;
      }else if(result.value.error||result.value.code)notice=`保存未完成：${result.value.error?.code||result.value.code}。草稿保留，请比较当前值。`;
    }catch{/* A missing receipt is never permission to repeat a command. */}
    finally{
      if(alive){
        if(body.identity)notices.set(body.identity,notice);operation.textContent=body.identity?'':notice;render();
        const checked=await load();
        if(alive&&online&&checked&&checked.process_instance_id===instance&&receipt&&checked.revision===receipt.revision&&checked.application_state===receipt.application_state&&!checked.external_change&&(!body.identity||checked.authorizations[body.identity]===body.authorization)){
          if(body.identity&&drafts.get(body.identity)===submitted)drafts.delete(body.identity);
          notice=checked.application_state==='applied'?'服务端确认操作，授权已生效。':'已保存，尚未生效；请显式重新应用。';
          if(body.identity)notices.set(body.identity,notice);operation.textContent=body.identity?'':notice;render();
        }
      }
      busy=false;submission=null;render();
    }
  }
  refresh.onclick=()=>void load();reapply.onclick=()=>void mutate('/api/tools/reapply',{expected_revision:current?.revision});
  const focus=()=>{if(!busy)void load();};window.addEventListener('focus',focus);
  reapply.disabled=true;void load();
  return {
    getLeaveState(){return {dirty:[...drafts].some(([identity,draft])=>!(submission?.identity===identity&&submission?.draft===draft)),summary:'工具授权草稿尚未确认保存。',pending:busy};},
    /** @param {string} [instance] */
    sync(instance){online=true;if(instance){expectedInstance=instance;if(current&&current.process_instance_id!==instance){verified=false;++generation;render();}}focus();},
    disconnect(){online=false;verified=false;++generation;readState.textContent='离线，当前生效状态待核验；草稿保留。';render();},
    close(){alive=false;++generation;controller.abort();window.removeEventListener('focus',focus);drafts.clear();rows.clear();groups.clear();retired.clear();}
  };
}
