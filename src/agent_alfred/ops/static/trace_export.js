import {node} from './dom.js';
/** @typedef {Record<string, any>} Wire */
/** @param {HTMLElement} root @param {string} runId @param {()=>string} csrf @param {import('./memory.js').MemorySync} memory */
export function traceExport(root, runId, csrf, memory) {
  const section = node('section');
  section.setAttribute('aria-label', '追踪导出');
  const mode = node('select');
  mode.setAttribute('aria-label','导出模式');
  for (const [value,label] of [['share','分享净化'],['diagnostic','保留诊断正文']]) {
    const option = node('option',label); option.value=value; mode.append(option);
  }
  const warning=node('p');
  const status=node('p'); status.setAttribute('role','status');
  const start=node('button','生成追踪导出');
  const cancel=node('button','取消导出');
  const download=node('button','下载 ZIP');
  section.append(node('h2','追踪导出'),mode,warning,start,cancel,download,status);
  root.append(section);
  /** @type {Wire|null} */ let task=null;
  let generation=0, disposed=false, downloading=false;
  let timer=0;
  let connection=memory.connections;
  let revision=memory.revision;
  /** @param {string} path @param {Wire} body @param {boolean} [keepalive] */
  async function post(path,body,keepalive=false) {
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','x-agent-alfred-csrf':csrf()},body:JSON.stringify(body),keepalive,cache:'no-store'});
    const data=await response.json();
    if (!response.ok) throw new Error(data.detail || data.code || '导出请求失败');
    return data;
  }
  function render() {
    warning.textContent=mode.value==='diagnostic'?'诊断正文仍可能含私人信息，分享前请自行检查。':'默认移除自由正文；净化与源追踪完整性分别说明。';
    start.disabled=!!task && task.cleanup!=='released' || !memory.online;
    cancel.hidden=!task || task.cleanup==='released';
    download.hidden=task?.state!=='ready' || downloading || !memory.online;
    mode.disabled=!!task && task.cleanup!=='released';
    if (task) {
      const labels=/** @type {Record<string,string>} */({generating:'正在校验并生成',ready:'文件已生成',sending:'已开始下载',cleaning:'正在清理',transferred:'服务端传输结束；不代表文件已保存',failed:'导出失败',cancelled:'已取消',invalidated:'已失效',expired:'已过期'});
      status.textContent=[labels[task.state]||'状态待核验',task.detail,task.cleanup==='failed'?'资源清理受阻，新导出暂停':'',task.source_integrity ? `源追踪完整性：${task.source_integrity}`:'',...(task.missing||[])].filter(Boolean).join(' · ');
    }
  }
  /** @param {Wire} stale */
  function discard(stale) {
    void post(`/api/trace-exports/${stale.task_id}/cancel`,{},true).catch(()=>{});
  }
  async function poll() {
    if (disposed || !task) return;
    const current=task, epoch=generation;
    try {
      const response=await fetch(`/api/trace-exports/${current.task_id}`,{cache:'no-store'});
      const next=await response.json();
      if (disposed || epoch!==generation || task!==current) return;
      if (!response.ok || next.instance_id!==memory.instance) throw new Error('导出状态不可确认，请重新生成');
      task=next; render();
      if (next.cleanup!=='released') timer=window.setTimeout(()=>void poll(),300);
    } catch {
      if (!disposed && epoch===generation) {
        download.hidden=true; status.textContent='连接中断，旧下载不可用；请重新核验。';
      }
    }
  }
  start.onclick=async()=> {
    if (!memory.online) return;
    const epoch=++generation, instance=memory.instance;
    start.disabled=true; status.textContent='正在发起导出';
    try {
      const next=await post('/api/trace-exports',{run_id:runId,mode:mode.value,instance_id:instance});
      if (disposed || epoch!==generation || instance!==memory.instance) {discard(next);return;}
      task=next; downloading=false; render(); void poll();
    } catch (error) {
      if (!disposed && epoch===generation) {status.textContent=error instanceof Error?error.message:'发起结果不可确认，请核验后手动重试';start.disabled=false;}
    }
  };
  cancel.onclick=async()=> {
    if (!task) return;
    const current=task, epoch=++generation;
    clearTimeout(timer); download.hidden=true;
    try {
      const next=await post(`/api/trace-exports/${current.task_id}/cancel`,{});
      if (disposed || generation!==epoch) return;
      task=next; render(); void poll();
    } catch {status.textContent='取消结果待核验，不能确认资源已释放';}
  };
  download.onclick=()=> {
    if (!task || task.state!=='ready' || !memory.online || downloading) return;
    const form=node('form');form.method='POST';form.action='/api/trace-exports/download';
    const target=`trace-download-${task.task_id}`;
    const frame=node('iframe'); frame.name=target;frame.hidden=true;
    form.target=target;
    for (const [name,value] of Object.entries({task_id:task.task_id,download_token:task.download_token,instance_id:task.instance_id,csrf:csrf()})) {
      const input=node('input');input.type='hidden';input.name=name;input.value=value;form.append(input);
    }
    document.body.append(frame,form);form.submit();form.remove();
    // Keep the native target alive across page navigation; it contains no ZIP buffer.
    frame.onload=()=>frame.remove();
    task.download_token=null;downloading=true;download.hidden=true;
    status.textContent='已请求原生下载；文件保存结果由浏览器确认。';void poll();
  };
  mode.onchange=render;
  const unwatch=memory.watch(()=> {
    if (!memory.online || memory.connections!==connection || memory.revision!==revision) {
      generation++;clearTimeout(timer);
      if (task && !downloading) discard(task);
      task=null;download.hidden=true;
      status.textContent='连接或记忆边界已变化，旧导出不可恢复；可重新手动生成。';
    }
    connection=memory.connections;revision=memory.revision;render();
  });
  function dispose() {
    if (disposed) return;
    disposed=true;generation++;clearTimeout(timer);unwatch();
    if (task && !downloading) discard(task);
    task=null;window.removeEventListener('pagehide',dispose);
  }
  window.addEventListener('pagehide',dispose);
  render();return {dispose};
}
