import {node} from './dom.js';
/** @typedef {Record<string, any>} Wire */
/** @typedef {{inputs:Wire,nodes:Wire[],edges:Wire[],routers:Wire[]}} Topology */
const kinds = /** @type {Record<string,string>} */ ({normal:'步骤依赖', conditional:'条件分支', error:'声明的恢复路径'});
/** @param {any} v */
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
/** @param {any} v */
const text = v => typeof v === 'string' && v.length > 0;
/** @param {any} v */
const strings = v => Array.isArray(v) && v.every(text) && new Set(v).size === v.length;

/** Reject the entire incompatible response before drawing any of it. @param {Wire} value @param {string} workflow */
export function validateSnapshot(value, workflow) {
  /** @param {any} condition */
  const require = condition => {if (!condition) throw new Error('incompatible');};
  require(object(value) && value.workflow === workflow && value.graph_id === workflow
    && text(value.process_instance_id) && Number.isSafeInteger(value.publication_generation)
    && value.publication_generation >= 0 && text(value.read_at) && Number.isFinite(Date.parse(value.read_at)));
  if (value.status === 'unavailable') {require(value.description === null); return value;}
  require(value.status === 'available' && value.publication_generation > 0);
  const d = value.description;
  require(object(d) && d.schema_version === 1 && typeof d.topology_hash === 'string'
    && /^[a-f0-9]{64}$/.test(d.topology_hash));
  const t = /** @type {Topology} */ (d.topology);
  require(object(t) && object(t.inputs) && Object.values(t.inputs).every(v=>typeof v === 'boolean')
    && Array.isArray(t.nodes) && t.nodes.length > 0 && Array.isArray(t.edges) && Array.isArray(t.routers));
  const ids = new Set(); let terminals = 0;
  for (const n of t.nodes) {
    require(object(n) && text(n.node_id) && !ids.has(n.node_id) && ['fn','llm','tool','agent'].includes(n.kind)
      && strings(n.required_reads) && strings(n.optional_reads) && strings(n.writes) && strings(n.tools)
      && typeof n.skippable_by_config === 'boolean');
    ids.add(n.node_id);
    if (n.terminal !== null) {
      const end = n.terminal;
      require(object(end) && (end.kind === 'result'
        ? text(end.output_key) && n.writes.includes(end.output_key) && end.reason_code === null
        : end.kind === 'no_action' && text(end.reason_code) && end.output_key === null));
      terminals++;
    }
  }
  require(terminals > 0);
  const edgeKeys = new Set();
  for (const e of t.edges) {
    require(object(e) && ids.has(e.source) && ids.has(e.target) && Object.hasOwn(kinds,e.kind)
      && (e.kind === 'conditional' ? text(e.label) : e.label === null));
    const key = JSON.stringify([e.source,e.target,e.kind,e.label]);
    require(!edgeKeys.has(key)); edgeKeys.add(key);
  }
  const sources = new Set(); const conditional = new Set();
  for (const r of t.routers) {
    require(object(r) && ids.has(r.source) && !sources.has(r.source) && object(r.path_map)
      && Object.keys(r.path_map).length > 0 && strings(r.required_reads) && strings(r.optional_reads));
    sources.add(r.source);
    for (const [label, target] of Object.entries(r.path_map)) {
      const targets = typeof target === 'string' ? [target] : target;
      require(text(label) && strings(targets) && targets.length > 0);
      for (const to of targets) {const key = JSON.stringify([r.source,to,'conditional',label]); require(edgeKeys.has(key)); conditional.add(key);}
    }
  }
  require(t.edges.filter(e=>e.kind === 'conditional').length === conditional.size);
  for (const n of t.nodes) if (n.terminal) require(!t.edges.some(e=>e.source === n.node_id && e.kind !== 'error'));
  // This also bounds the layout walk: cyclic or dangling structure is never partially displayed.
  const settled = new Set();
  while (settled.size < ids.size) {
    const ready = t.nodes.filter(n=>!settled.has(n.node_id) && t.edges.filter(e=>e.target === n.node_id).every(e=>settled.has(e.source)));
    require(ready.length > 0); ready.forEach(n=>settled.add(n.node_id));
  }
  return value;
}

/** @param {Wire} d @param {Wire} n */
function caption(d,n) {
  const names=d.presentation?.nodes;
  const p=names && Object.hasOwn(names,n.node_id) ? names[n.node_id] : null;
  return {name: typeof p?.name === 'string' ? p.name : n.node_id,
    description: typeof p?.description === 'string' ? p.description : '暂无说明'};
}
/** @param {Wire} n */
function terminal(n) {
  return n.terminal ? n.terminal.kind === 'result' ? `结果出口 · ${n.terminal.output_key}`
    : `无动作出口 · ${n.terminal.reason_code}（不产生内容）` : '步骤';
}
/** @param {Wire} d @param {Wire} e */
function edgeText(d,e) {
  const branches=d.presentation?.branches;
  const labels=branches && Object.hasOwn(branches,e.source) ? branches[e.source] : null;
  const meaning=labels && Object.hasOwn(labels,e.label) ? labels[e.label] : null;
  return `${e.source} → ${e.target} · ${kinds[e.kind]}${e.label ? ` · ${e.label}：${typeof meaning === 'string' ? meaning : '暂无说明'}` : ''}`;
}
/** @param {string} tag @param {Record<string,string|number>} attrs @param {string} [content] */
function svgNode(tag,attrs,content) {
  const el = document.createElementNS('http://www.w3.org/2000/svg',tag);
  for (const [k,v] of Object.entries(attrs)) el.setAttribute(k,String(v));
  if (content) el.textContent = content;
  return el;
}

/** Independent observation component; it never owns or rebuilds business controls.
 * @param {HTMLElement} root @param {string} workflow @param {()=>Wire} runtime
 * @param {import('./memory.js').MemorySync} sync
 * @param {string|null} [runId]
 */
export function topologyView(root, workflow, runtime, sync, runId=null) {
  const pathMode=runId!==null;
  const openLabel=pathMode?'本次执行路径':'查看流程';
  const panel = node('section'); panel.className='topology'; panel.setAttribute('aria-label',pathMode?'本次执行路径':'流程拓扑');
  const toggle = node('button',openLabel); toggle.setAttribute('aria-expanded','false');
  const content = node('div'); content.hidden=true; content.id=`topology-${workflow}`;
  toggle.setAttribute('aria-controls',content.id);
  const status=node('p'); status.setAttribute('role','status');
  const connection=node('p'); connection.setAttribute('role','status');
  const refresh=node('button','重新读取');
  const drawing=node('div'); const details=node('div');
  content.append(refresh,connection,status,drawing,details); panel.append(toggle,content); root.append(panel);
  let expanded=false, closed=false, sequence=0, selected='';
  let knownInstance=runtime().instance || '', online=runtime().connected;
  /** @type {Wire|null} */ let snapshot=null;
  let phase='idle', changed=false, unavailableReason='';
  let viewport={x:0,y:0,width:1000,height:600}; let full={...viewport};
  /** @type {SVGElement|null} */ let svg=null;
  /** @type {AbortController|null} */ let pending=null;
  /** @type {HTMLElement|null} */ let nodeDetail=null;
  function invalidate() {sequence++; pending?.abort(); pending=null;}
  function viewbox() {svg?.setAttribute('viewBox',`${viewport.x} ${viewport.y} ${viewport.width} ${viewport.height}`);}
  function fit() {viewport={...full}; viewbox();}
  /** @param {number} factor */
  function zoom(factor) {
    const width=Math.max(full.width/8,Math.min(full.width*4,viewport.width*factor));
    const height=viewport.height*width/viewport.width;
    viewport={x:viewport.x+(viewport.width-width)/2,y:viewport.y+(viewport.height-height)/2,width,height}; viewbox();
  }
  /** @param {Wire} n */
  function nodeState(n) {
    if (!pathMode || !snapshot) return '';
    const f=snapshot.nodes.find((/** @type {Wire} */ f)=>f.node_id===n.node_id);
    return f ? ` · ${pathState(f.state)} · ${pathState(f.commit)}${f.reason?' · '+f.reason:''}` : ' · 未知';
  }
  /** @param {Wire} e */
  function connectionText(e) {
    const base=edgeText(snapshot?.description,e);
    if (!pathMode || !snapshot) return base;
    const f=snapshot.edges.find((/** @type {Wire} */ f)=>['source','target','kind','label'].every(k=>e[k]===f[k]));
    return base+' · '+pathState(f?.state||'unknown');
  }
  function notices() {
    connection.textContent=online ? '服务已连接；结构仅在手动读取时更新。' : '服务连接已断开；保留的图仅为旧快照。';
    const oldProcess=snapshot && knownInstance && snapshot.process_instance_id !== knownInstance;
    const prefix=snapshot ? `${oldProcess ? '来自上一服务实例 · 旧快照；' : ''}${pathMode?'读取时的执行路径':'读取时的结构'} · ${snapshot.read_at}。` : '';
    const pathNotice=pathMode && snapshot ? `来源：${snapshot.source==='process'?'本次进程观测，尚未保证保存':'持久 trace'}；路径 ${snapshot.path_status}；Graph ${snapshot.graph_outcome||'尚未观察到终态'}；Run ${snapshot.run.outcome||snapshot.run.phase}；记录 ${snapshot.run.recording_state||'未落定'}。${snapshot.graph_reason||''}` : '';
    /** @type {Record<string,string>} */
    const messages={idle:'',loading:snapshot ? '旧快照 · 重新读取中…' : '正在读取…',ready:'',failed:snapshot ? '旧快照 / 本次读取失败，请重新读取。' : '首次读取失败，请重试读取。',incompatible:snapshot ? '上次成功的旧快照；当前响应无法展示（不兼容），请重新读取。' : '当前响应无法展示（不兼容），请重试读取。',unavailable:(pathMode?'路径不可用。':'当前没有已发布的图。') + unavailableReason};
    status.textContent=prefix + pathNotice + (messages[phase] || '') + (changed ? '结构已更新，已适应全图并关闭旧节点详情。' : '');
    refresh.textContent=(!snapshot && ['failed','incompatible'].includes(phase)) ? '重试读取' : '重新读取';
  }
  function showNode() {
    if (!nodeDetail || !snapshot) return;
    nodeDetail.replaceChildren();
    const d=snapshot.description, n=d.topology.nodes.find((/** @type {Wire} */ n)=>n.node_id === selected);
    if (!n) return;
    const c=caption(d,n);
    nodeDetail.append(node('h4',`${c.name} · ${n.node_id}`),node('p',c.description),node('p',terminal(n)+nodeState(n)),node('pre',JSON.stringify(n,null,2)));
    if (pathMode) {
      const f=snapshot.nodes.find((/** @type {Wire} */ f)=>f.node_id===selected);
      for (const ref of f?.references||[]) {
        const target=ref.attempt_id ? `attempt-${ref.attempt_id}` : `step-${ref.step_index}`;
        const label=ref.attempt_id?`Attempt ${ref.attempt_id}`:`Step ${ref.step_index}`;
        if (document.getElementById(target)) {
          const link=node('a',label); link.href='#'+encodeURIComponent(target);
          link.addEventListener('click',event=>{
            event.preventDefault(); event.stopPropagation();
            const evidence=document.getElementById(target);
            if (!evidence) {link.replaceWith(node('p',label+' · 关联证据不可用')); return;}
            if(evidence instanceof HTMLDetailsElement) evidence.open=true;
            evidence.tabIndex=-1; evidence.focus(); evidence.scrollIntoView({block:'center'});
          });
          nodeDetail.append(link);
        } else nodeDetail.append(node('p',label+' · 关联证据不可用'));
      }
    }
  }
  /** @param {string} id */
  function select(id) {selected=id; showNode();}
  /** @param {boolean} reset */
  function draw(reset) {
    if (!snapshot) return;
    drawing.replaceChildren(); details.replaceChildren();
    const d=snapshot.description;
    const t=/** @type {Topology} */ (d.topology);
    const controls=node('div'); controls.className='topology-controls';
    for (const [label,action] of /** @type {[string,()=>void][]} */ ([['放大',()=>zoom(.8)],['缩小',()=>zoom(1.25)],['适应全图',fit],['向左平移',()=>{viewport.x-=viewport.width/5;viewbox();}],['向右平移',()=>{viewport.x+=viewport.width/5;viewbox();}],['向上平移',()=>{viewport.y-=viewport.height/5;viewbox();}],['向下平移',()=>{viewport.y+=viewport.height/5;viewbox();}]])) {
      const b=node('button',label); b.addEventListener('click',action); controls.append(b);
    }
    drawing.append(controls,node('p','图例：实线＝步骤依赖；虚线＝条件分支；点线＝声明的恢复路径（不保证任意异常均可恢复）。出口以文字标明。'));
    const positions=new Map(); let remaining=[...t.nodes], rank=0, maxRows=1;
    while (remaining.length) {
      const wave=remaining.filter(n=>t.edges.filter(e=>e.target===n.node_id).every(e=>positions.has(e.source)));
      wave.forEach((n,i)=>positions.set(n.node_id,{x:60+rank*350,y:80+i*160}));
      maxRows=Math.max(maxRows,wave.length); remaining=remaining.filter(n=>!positions.has(n.node_id)); rank++;
    }
    full={x:0,y:0,width:rank*350+30,height:maxRows*160+80}; if (reset) viewport={...full};
    svg=svgNode('svg',{'aria-label':'流程节点与连接图',role:'img'}); svg.classList.add('topology-canvas');
    const defs=svgNode('defs',{}), marker=svgNode('marker',{id:`arrow-${workflow}`,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});
    marker.append(svgNode('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'currentColor'})); defs.append(marker); svg.append(defs);
    t.edges.forEach((e,i)=>{
      const a=positions.get(e.source), b=positions.get(e.target), offset=(i%3-1)*16;
      // Long dependencies travel above the intervening nodes, so passing a
      // recovery node cannot look like an undeclared connection to that node.
      const path=b.x-a.x>350
        ? `M ${a.x+240} ${a.y+35+offset} H ${a.x+265+i*2} V ${16+i*3} H ${b.x-25-i*2} V ${b.y+35+offset} H ${b.x}`
        : `M ${a.x+240} ${a.y+35+offset} C ${a.x+290} ${a.y+35+offset}, ${b.x-45} ${b.y+35+offset}, ${b.x} ${b.y+35+offset}`;
      const line=svgNode('path',{d:path,'data-graph-edge':JSON.stringify(e),
        fill:'none',stroke:'currentColor','stroke-width':2,'stroke-dasharray':e.kind==='conditional'?'9 5':e.kind==='error'?'2 5':'none','marker-end':`url(#arrow-${workflow})`});
      line.append(svgNode('title',{},connectionText(e))); svg?.append(line);
      if (e.label) svg?.append(svgNode('text',{x:(a.x+240+b.x)/2,y:(a.y+b.y)/2+20+offset,'font-size':12},e.label));
    });
    for (const n of t.nodes) {
      const p=positions.get(n.node_id), c=caption(d,n);
      const g=svgNode('g',{'data-node-id':n.node_id});
      g.append(svgNode('rect',{x:p.x,y:p.y,width:240,height:90,rx:8,fill:'var(--topology-node, #f5f7fa)',stroke:'currentColor'}),
        svgNode('text',{x:p.x+10,y:p.y+25,'font-size':14},c.name),svgNode('text',{x:p.x+10,y:p.y+49,'font-size':11},n.node_id),
        svgNode('text',{x:p.x+10,y:p.y+74,'font-size':12},pathMode?nodeState(n):n.terminal ? n.terminal.kind==='result'?'结果出口':'无动作出口':'步骤'));
      svg.append(g);
    }
    /** @type {null|{pointer:number,x:number,y:number,view:typeof viewport,nodeId:string,moved:boolean}} */
    let drag=null;
    svg.addEventListener('pointerdown',event=>{
      const e=/** @type {PointerEvent} */ (event);
      if (!e.isPrimary || e.button!==0 || drag) return;
      // Capture retargets pointerup to the SVG, so remember the original node.
      const target=e.target instanceof Element ? e.target.closest('[data-node-id]') : null;
      drag={pointer:e.pointerId,x:e.clientX,y:e.clientY,view:{...viewport},nodeId:target?.getAttribute('data-node-id')||'',moved:false};
      svg?.setPointerCapture(e.pointerId);
    });
    svg.addEventListener('pointermove',event=>{
      const e=/** @type {PointerEvent} */ (event);
      if (!drag || !svg || e.pointerId!==drag.pointer) return;
      const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
      if (Math.hypot(dx,dy)>=4) drag.moved=true;
      if (!drag.moved) return;
      const box=svg.getBoundingClientRect(),scale=Math.max(drag.view.width/box.width,drag.view.height/box.height);
      viewport={...drag.view,x:drag.view.x-dx*scale,y:drag.view.y-dy*scale};viewbox();
    });
    svg.addEventListener('pointerup',event=>{
      const e=/** @type {PointerEvent} */ (event);
      if (!drag || e.pointerId!==drag.pointer) return;
      if (!drag.moved && Math.hypot(e.clientX-drag.x,e.clientY-drag.y)<4 && drag.nodeId) select(drag.nodeId);
      drag=null;
    });
    const cancel=()=>{drag=null;};
    svg.addEventListener('pointercancel',cancel);svg.addEventListener('lostpointercapture',cancel);
    drawing.append(svg);viewbox();
    details.append(node('h3','完整文字明细'),node('p',pathMode?'仅展示读取边界内可证明的事实。taken 不证明目标成功；波撤销不撤回工具副作用或费用。':'这是声明结构，不代表某次运行已经执行、成功或跳过。图存在不承诺模型/工具可用或下一次运行成功。'));
    if (typeof d.presentation?.policy==='string') details.append(node('p',d.presentation.policy));
    const identity=node('details'); identity.append(node('summary','快照身份与输入声明'),node('pre',JSON.stringify({workflow:snapshot.workflow,graph_id:snapshot.graph_id,process_instance_id:snapshot.process_instance_id,publication_generation:snapshot.publication_generation,schema_version:d.schema_version,topology_hash:d.topology_hash,inputs:t.inputs,...(pathMode?{run_id:runId,original_identity:snapshot.identity,source:snapshot.source,boundary:snapshot.boundary,run:snapshot.run}: {})},null,2))); details.append(identity);
    const nodes=node('ul');
    for (const n of t.nodes) {
      const c=caption(d,n), li=node('li'), b=node('button',`${c.name} · ${n.node_id}`);
      b.addEventListener('click',()=>select(n.node_id)); li.append(b,node('p',c.description),node('p',terminal(n)+nodeState(n))); nodes.append(li);
    }
    nodeDetail=node('div'); nodeDetail.setAttribute('aria-label','节点详情');
    details.append(nodes,nodeDetail,node('h4','全部连接与条件'));
    const edges=node('ul');
    for (const e of t.edges) {const li=node('li',connectionText(e));li.dataset.topologyEdge=JSON.stringify(e);edges.append(li);}
    details.append(edges);
    if (pathMode) {
      details.append(node('h4','按 wave 分组'));
      for (const wave of snapshot.waves) details.append(node('p',`Wave ${wave.wave} · ${pathState(wave.state)} · ${wave.nodes.join(', ')}${wave.reason?' · '+wave.reason:''}`));
      for(const stage of snapshot.stages||[]) details.append(node('p',`${stage.stage==='fallback'?'图外回退':'图前绕行'} · ${stage.entered?'已进入':'已阻止'} · ${stage.reason}`));
      details.append(node('h4','独立业务摘要'),node('pre',JSON.stringify({stages:snapshot.stages,...snapshot.summary},null,2)));
    }
    const routers=node('details');routers.append(node('summary','条件映射与读取声明'),node('pre',JSON.stringify(t.routers,null,2)));details.append(routers);showNode();
  }
  async function read() {
    invalidate(); const request=sequence;
    pending=new AbortController();phase='loading';changed=false;notices();
    try {
      const response=await fetch(pathMode?'/api/run-path?run_id='+encodeURIComponent(runId||''):'/api/behaviour/topology?workflow='+encodeURIComponent(workflow),{signal:pending.signal});
      if (!response.ok) throw new Error('read_failed');
      let body=await response.json();
      if (closed || !expanded || !panel.isConnected || request!==sequence) return;
      if (knownInstance && (pathMode?body.service_instance_id:body.process_instance_id)!==knownInstance) throw new Error('read_failed');
      if (pathMode) {
        try {body=validatePath(body,runId||'');} catch {phase='incompatible';notices();return;}
      }
      let next;
      try {next=validateSnapshot(body,pathMode?body.workflow:workflow);} catch {phase='incompatible';notices();return;}
      if (next.status==='unavailable') {
        snapshot=pathMode?next:null;selected='';drawing.replaceChildren();details.replaceChildren();phase='unavailable';
        unavailableReason=pathMode ? pathReason(next.reason) : next.reason==='graph_not_published' ? '原因：图未成功发布。' : '原因未知。';
        if (pathMode && next.summary) details.append(node('h4','独立业务摘要'),node('pre',JSON.stringify({stages:next.stages,...next.summary},null,2)));
        notices();return;
      }
      changed=Boolean(snapshot?.description && (snapshot.description.topology_hash!==next.description.topology_hash || snapshot.description.schema_version!==next.description.schema_version));
      const reset=!snapshot?.description || changed;
      if (reset) selected='';snapshot=next;phase='ready';draw(reset);notices();
    } catch {
      if (closed || !expanded || !panel.isConnected || request!==sequence) return;
      phase='failed';notices();
    }
  }
  toggle.addEventListener('click',()=>{
    expanded=!expanded; content.hidden=!expanded; toggle.textContent=expanded?(pathMode?'收起执行路径':'收起流程'):openLabel;toggle.setAttribute('aria-expanded',String(expanded));
    if (expanded) void read(); else invalidate();
  });
  refresh.addEventListener('click',()=>void read());
  const unwatch=sync.watch(()=>{
    if (closed) return;
    const current=runtime();
    if (current.instance && current.instance!==knownInstance) {
      const previous=knownInstance; knownInstance=current.instance;
      // Learning the first identity is not a process change. An in-flight
      // response still validates against this identity when it arrives.
      if (previous || (snapshot && snapshot.process_instance_id!==knownInstance)) {
        invalidate(); phase=snapshot?'ready':'failed';
      }
    }
    if (online && !current.connected) {invalidate();if(phase==='loading') phase='failed';}
    online=current.connected;notices();
  });
  notices();
  return {close(){closed=true;invalidate();unwatch();}};
}

/** @param {string} value */
function pathState(value) {
  return /** @type {Record<string,string>} */ ({unknown:'未知',not_started:'未开始（本次证据范围）',started:'已开始，尚未观察到终态',succeeded:'计算成功',failed:'失败',skipped:'跳过',aborted:'撤销',committed:'已提交',pending:'尚未证明波提交',taken:'已选中 taken',not_taken:'未选中 not_taken',undecided:'尚未决议'})[value]||value;
}
/** @param {string} value */
function pathReason(value) {
  return /** @type {Record<string,string>} */ ({pruned:'已裁剪',missing:'缺失',unavailable:'缺失或不可读取',corrupt:'证据损坏或身份/顺序矛盾',unsupported:'不支持的格式',too_large:'超过历史 32 MiB 读取上限',current_limit:'超过当前观测缓存上限',observation_failed:'观测失败',graph_bypassed:'图前绕行，未进入图',no_historical_structure:'缺少历史结构，不能使用当前图补齐'})[value]||value;
}
/** @param {Wire} v @param {string} runId */
function validatePath(v,runId) {
  if(v.run_id!==runId || v.evidence_version!==1 || !text(v.service_instance_id) || !text(v.read_at)) throw new Error('identity');
  if(v.status==='unavailable') return {...v,path_status:v.status,workflow:runId,graph_id:runId,process_instance_id:v.service_instance_id,publication_generation:0,description:null};
  const id=v.identity;
  if(!id || id.run_id!==runId || !text(id.process_instance_id) || id.evidence_version!==1 || !['available','partial'].includes(v.status) || !['trace','process'].includes(v.source) || !v.boundary || !Array.isArray(v.nodes) || !Array.isArray(v.edges) || !Array.isArray(v.waves)) throw new Error('evidence');
  if(id.topology_hash!==v.description?.topology_hash || id.schema_version!==v.description?.schema_version) throw new Error('topology');
  const ids=v.description.topology.nodes.map((/** @type {Wire} */ n)=>n.node_id);
  if(v.nodes.length!==ids.length || new Set(v.nodes.map((/** @type {Wire} */ n)=>n.node_id)).size!==ids.length || v.nodes.some((/** @type {Wire} */ n)=>!ids.includes(n.node_id))) throw new Error('nodes');
  const edgeKey=(/** @type {Wire} */ e)=>JSON.stringify([e.source,e.target,e.kind,e.label]);
  const edgeIds=v.description.topology.edges.map(edgeKey);
  if(v.edges.length!==edgeIds.length || new Set(v.edges.map(edgeKey)).size!==edgeIds.length || v.edges.some((/** @type {Wire} */ e)=>!edgeIds.includes(edgeKey(e))||!['taken','not_taken','unknown','undecided'].includes(e.state))) throw new Error('edges');
  if(v.nodes.some((/** @type {Wire} */ n)=>!['unknown','not_started','started','succeeded','failed','skipped','aborted'].includes(n.state)||!['unknown','pending','committed','aborted'].includes(n.commit)||!Array.isArray(n.references))) throw new Error('node states');
  const waveNodes=v.waves.flatMap((/** @type {Wire} */ w)=>w.nodes);
  if(waveNodes.length!==ids.length || new Set(waveNodes).size!==ids.length || waveNodes.some((/** @type {string} */ n)=>!ids.includes(n)) || v.waves.some((/** @type {Wire} */ w,/** @type {number} */ i)=>w.wave!==i||!['unknown','committed','aborted'].includes(w.state))) throw new Error('waves');
  return {...v,status:'available',path_status:v.status,workflow:id.workflow,graph_id:id.graph_id,process_instance_id:v.service_instance_id,publication_generation:id.publication_generation};
}
