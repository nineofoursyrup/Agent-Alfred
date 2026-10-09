import {createShell, pageDefinition, TabStorage} from "./shell.js";
import {aggregationFacts} from "./aggregation.js";
import { toolsPage } from "./tools.js";
import { accountingPage } from "./accounting.js";
import { Stream } from "./stream.js";
import { node, textBlocks } from "./dom.js";
import { Progress } from "./progress.js";
import { ConnectionNotices, Announcer } from "./notices.js";
import { modelsPage, connectionsPage, behaviourPage } from "./pages.js";
import { inbox } from "./inbox.js";
import { runsPage, outcomeLabel } from "./runs.js";
import { MemorySync, memoryPage, memoryReceipts } from "./memory.js";
import { databasePage } from "./database.js";
import { overviewPage } from "./overview.js";
/** @typedef {Record<string, any>} Wire */
/** @template {Element} T @param {string} id @returns {T} */
function element(id) {
  const result = document.getElementById(id);
  if (!result) throw new Error(`Missing element ${id}`);
  return /** @type {T} */ (/** @type {unknown} */ (result));
}
const input = /** @type {HTMLTextAreaElement} */ (element("message"));
const drawer = /** @type {HTMLElement} */ (element("conversation"));
const error = element("error");
const storage = new TabStorage(() => {element("storage-warning").textContent="存储不可用：输入仅在本页临时保留，刷新可能无法恢复。";});
let session = storage.get("alfred.session");
let csrf = "";
let instance = "";
let revision = -1;
let connected = false;
/** @type {{page:ReturnType<typeof runsPage>,generation:number}|null} */ let restoreRunPage=null;
/** @type {ReturnType<typeof toolsPage>|ReturnType<typeof accountingPage>|null} */ let accountingView = null;
let valid = false;
let sending = false;
let creating = false;
let createdSessionRevision = 0;
/** Successful creations whose original selection intent can no longer apply.
 * @type {Map<string,{session_id:string,instance:string,reason:string}>} */
const createdSessions = new Map();
/** @type {Set<()=>void>} */ const sessionActionListeners = new Set();
let unavailable = false;
/** @type {Wire|null} */ let busySummary = null;
/** @type {Wire|null} */ let active = null;
/** Current immutable Host projection, distinct from MainBar's retained reply records. */
/** @type {Wire|null} */ let hostProjection = null;
let readGapRevision = 0;
/** @type {Map<string, Wire>} */ const replies = new Map();
/** @type {Wire[]} */ let historyItems = [];
let historyCursor = "";
let historyLoaded = false;
let historyRequest = 0;
let sessionGeneration=0;
let historyPending = false;
let historyLoading = false;
let historyRefresh = false;
const progress = new Progress();
const memory = new MemorySync();
const receipts = memoryReceipts(memory, () => csrf);
/** @type {ReturnType<typeof runsPage>|null} */ let runPage = null;
/** @type {ReturnType<typeof connectionsPage>|null} */ let connectionsView = null;
/** @type {ReturnType<typeof databasePage>|null} */ let databaseView = null;
/** @type {ReturnType<typeof behaviourPage>|null} */ let behaviourView = null;
const notices = new ConnectionNotices(element("connection"));
const announcer = new Announcer(element("announcements"));
const alerted = new Set();
/** @type {Map<string, Promise<void>>} */ const recovering = new Map();

/** @param {string} runId */
async function recoverReply(runId) {
  const target = session;
  const generation=sessionGeneration;
  const process = instance;
  if (target === null) return;
  const key = JSON.stringify([process, target, runId]);
  if (recovering.has(key)) return recovering.get(key);
  const operation = (async () => {
    try {
      const response = await fetch(
        "/api/reply?" +
          new URLSearchParams({
            process_instance_id: process,
            session_id: target,
            run_id: runId,
          }),
      );
      const result = await response.json();
      if (session !== target || instance !== process || generation!==sessionGeneration) return;
      const reply = replies.get(runId);
      if (!reply || reply.session_id !== target) return;
      if (
        !response.ok ||
        result.process_instance_id !== process ||
        result.session_id !== target ||
        result.run_id !== runId ||
        (typeof result.reply_text !== "string" && result.reply_disposition !== "no_reply")
      )
        throw new Error("正文未完整加载");
      reply.reply_disposition = result.reply_disposition;
      reply.text = result.reply_disposition === "no_reply" ? undefined : result.reply_text;
      reply.skill_notice = result.skill_notice;
      reply.loading = false;
    } catch {
      if (instance === process && session === target && generation===sessionGeneration) {
        const reply = replies.get(runId);
        if (reply) reply.loading = true;
      }
    } finally {
      recovering.delete(key);
      renderMessages();
    }
  })();
  recovering.set(key, operation);
  return operation;
}

function canSend() {
  return (
    !!csrf &&
    connected &&
    valid &&
    !active &&
    !busySummary &&
    !unavailable &&
    !sending &&
    !creating &&
    session !== null &&
    !!input.value.trim()
  );
}
function createSessionReason() {
  return sending ? '正在提交，请等待当前请求确认。'
    : !csrf ? '入口凭据尚未就绪，请等待连接核验。'
    : active || busySummary ? '当前运行尚未收尾，请完成后再新建。'
    : unavailable ? '记录服务不可用，暂不能新建会话。' : '';
}
/** The one owner supplies both control availability and click-time checks.
 * @param {'create'|'continue'} action @param {string|null} [target] */
function sessionActionState(action, target = null) {
  let reason = creating ? '正在新建会话，请等待完成。'
    : sending ? '正在提交，请等待当前请求确认。'
    : !csrf ? '入口凭据尚未就绪，请等待连接核验。' : '';
  if (!reason && action === 'create') {
    reason = createSessionReason();
  } else if (!reason && action === 'continue') {
    reason = !connected ? '连接尚未同步，请等待状态核验。'
      : typeof target !== 'string' ? '会话身份不可用。'
      : (['chat','aggregation'].includes(active?.purpose) && active?.session_id === session && target !== session)
        ? '当前会话的运行尚未收尾，请完成后再切换。'
      : target === session && !valid ? '会话已失效；请保留草稿并选择或新建会话。' : '';
  }
  return {allowed: !reason, reason, createdRevision: createdSessionRevision};
}
function updateSend() {
  /** @type {HTMLButtonElement} */ (element("send")).disabled = !canSend();
  element("send-reason").textContent=creating?"正在新建会话；草稿仍可编辑，不会排队发送。":session===null?"请显式新建或继续会话。":!connected?"连接尚未同步；草稿仍可编辑。":!valid?"会话不可用；请保留草稿并选择会话。":unavailable?"记录服务不可用；不会排队发送。":sending?"正在提交；新增输入会保留。":active||busySummary?"当前运行尚未收尾；不会排队发送。":"Enter 发送 · Shift+Enter 换行";
  /** @type {HTMLButtonElement} */ (element("new-session")).disabled =
    !sessionActionState('create').allowed;
  for (const listener of sessionActionListeners) listener();
  if(createdSessions.size)renderShellStatus();
}
/** @param {Wire|null} summary */
function renderBusy(summary) {
  busySummary = summary;
  const card = /** @type {HTMLElement} */ (element("busy"));
  card.hidden = !summary;
  card.replaceChildren();
  if (!summary) return;
  card.append(
    node("strong", summary.stage),
    node(
      "p",
      summary.purpose === "chat" && summary.prompt_preview
        ? summary.prompt_preview
        : summary.purpose,
    ),
  );
  card.append(
    node(
      "small",
      `${summary.gateway === "cli" ? "CLI" : summary.gateway === "web" ? "Web" : summary.gateway} · ${summary.started_at || "尚未开始"}${summary.current_step === null ? "" : " · Step " + summary.current_step}`,
    ),
  );
  const href = summary.navigation?.href;
  if (typeof href === "string") {
    const url = new URL(href, location.origin);
    if (url.origin === location.origin && url.pathname.startsWith("/runs/")) {
      const link = node("a", "查看当前运行");
      link.href = url.href;
      card.append(link);
    }
  }
}
/** @type {Map<string,{element:HTMLElement,signature:string}>} */ const messageNodes=new Map();
/** @type {Wire|null} */ let locationTarget=null;
let locateGeneration=0;
/** @type {AbortController|null} */ let locateController=null;
/** @type {Wire|null} */ let locatedRecord=null;
let followLatest=true;
let readingIntent=0;
for(const kind of ['focusin','pointerdown','input','keydown','wheel','touchstart'])document.addEventListener(kind,()=>{readingIntent++;},{passive:true});
/** @type {Wire|null} */ let terminalStatus=null;
/** @type {Wire|null} */ let processGap=null;
/** @type {Set<string>} */ const notifiedReplies=new Set();
/** @type {{run_id:string,seen:boolean}|null} */ let unread=null;
function restoreUnread() {
  unread=null;
  if(session===null)return;
  try {const value=JSON.parse(storage.get(`alfred.unread:${session}`)||'null');if(value && typeof value.run_id==='string'){unread={run_id:value.run_id,seen:value.seen===true};notifiedReplies.add(value.run_id);}}catch{/* Invalid non-body metadata is discarded. */}
}
function saveUnread() {if(session!==null && unread)storage.set(`alfred.unread:${session}`,JSON.stringify(unread));}
/** @param {string} runId @param {Wire} reply */
function noteReply(runId,reply) {
  if(reply.reply_disposition==='no_reply' || reply.session_id!==session || notifiedReplies.has(runId))return;
  notifiedReplies.add(runId);unread={run_id:runId,seen:false};saveUnread();
}
function updateReadStatus() {
  const latest=unread?messageNodes.get('run:'+unread.run_id)?.element:null;
  if(unread && !unread.seen && !locationTarget && latest?.dataset.replyComplete==='true' && document.visibilityState==='visible' && shell.visibleMainbar()) {
    const view=drawer.getBoundingClientRect();const end=latest.getBoundingClientRect();
    if(end.bottom<=view.bottom+1 && end.bottom>view.top && end.height>0){unread.seen=true;saveUnread();}
  }
  const status=element('reading-status');
  status.textContent=locationTarget ? (locationTarget.loading?'正在定位指定记录…':locationTarget.error || `已定位历史${locationTarget.purpose==='aggregation'?locationTarget.reply_disposition==='no_reply'?'聚合记录':'聚合草稿':locationTarget.reply_disposition==='no_reply'?'记录':'回复'}；相邻历史尚未读取。`) : '';
  /** @type {HTMLButtonElement} */(element('retry-location')).hidden=!locationTarget?.error;
  /** @type {HTMLButtonElement} */(element('return-latest')).hidden=!locationTarget && !(unread && !unread.seen) && followLatest;
  renderShellStatus();
}
function renderShellStatus() {
  const container=element('shell-status');
  // Keep deferred receipt controls attached: ordinary Host updates must not
  // discard their focus while their availability changes.
  for(const child of Array.from(container.children)){
    const key=child.getAttribute('data-created-receipt');
    if(key===null || !createdSessions.has(key))child.remove();
  }
  const status=document.createDocumentFragment();
  const facts=[];
  if(!connected)facts.push('连接中断或正在同步');
  if(busySummary)facts.push(`${busySummary.stage} · ${busySummary.gateway==='cli'?'CLI':busySummary.gateway==='web'?'Web':busySummary.gateway||'来源未知'}`);
  if(unavailable)facts.push('记录服务不可用 · 未保存');
  if(terminalStatus && !busySummary){
    const latest=replies.get(terminalStatus.run_id)||terminalStatus;
    facts.push(terminalStatus.outcome==='completed' && terminalStatus.reply_disposition==='no_reply' ? terminalStatus.aggregation?'聚合已结束 · 未生成草稿':outcomeLabel(terminalStatus) : outcomeLabel({...terminalStatus,reply_disposition:undefined}));
    facts.push(latest.recording_unverified?'记录状态待核对':latest.recording_state==='recorded'?'已保存':latest.recording_state==='failed'?'未保存':latest.recording_state==='pending'?'正在保存':'记录状态待核对');
  }
  if(processGap)facts.push(processGap.message);
  if(locationTarget?.error)facts.push(locationTarget.error);
  if([...replies.values()].some(reply=>reply.session_id===session && reply.loading))facts.push('正文未完整加载');
  if(unread&&!unread.seen)facts.push(messageNodes.get('run:'+unread.run_id)?.element.dataset.replyComplete==='true'?'有新回复':'有新回复 · 结果需核对');
  if(element('storage-warning').textContent)facts.push('本页临时保留，刷新恢复受限');
  if(!facts.length)facts.push(session===null?'尚未选择会话':'已同步');
  status.append(node('p',facts.join(' · ')));
  if(busySummary?.navigation?.href){const link=node('a','查看当前运行');link.href=busySummary.navigation.href;status.append(link);}
  if(terminalStatus && !busySummary){const link=node('a','查看最近运行结果');link.href='/runs/'+encodeURIComponent(terminalStatus.run_id);status.append(link);}
  if(processGap && typeof processGap.run_id==='string'){const link=node('a','查看过程缺口');link.href='/runs/'+encodeURIComponent(processGap.run_id);status.append(link);}
  if(unread&&!unread.seen){const show=node('button','查看新回复');show.onclick=()=>{shell.openPanel('mainbar');returnLatest();};status.append(show);}
  if(unread&&!unread.seen && messageNodes.get('run:'+unread.run_id)?.element.dataset.replyComplete!=='true'){
    const verify=node('button','核对未读结果');verify.onclick=()=>{if(session!==null&&unread)void locateReply({process_instance_id:instance,session_id:session,run_id:unread.run_id,action_id:crypto.randomUUID()});};status.append(verify);
  }
  container.insertBefore(status,container.firstChild);
  for(const [key,receipt] of createdSessions) {
    let box=Array.from(container.children).find(child=>child.getAttribute('data-created-receipt')===key);
    if(!box) {
      box=node('section');box.setAttribute('aria-label','新建会话回执');box.setAttribute('data-created-receipt',key);
      box.append(node('p',`会话 ${receipt.session_id === '' ? '（空标识）' : receipt.session_id} 已创建，尚未切换。`),node('p',`当时未切换：${receipt.reason}`));
      const availability=node('p');availability.setAttribute('data-created-availability','');box.append(availability);
      const open=node('button','打开已创建会话');
      open.onclick=()=>{if(receipt.instance===instance)void continueSession(receipt.session_id);};
      const inspect=node('a','查看已创建会话');inspect.href='/inbox?'+new URLSearchParams({session_id:receipt.session_id,view:'messages'});
      box.append(open,inspect);container.append(box);
    }
    const reason=receipt.instance!==instance ? '进程已变化，请先查看并核验已创建会话。' : sessionActionState('continue',receipt.session_id).reason;
    /** @type {HTMLElement} */(box.querySelector('[data-created-availability]')).textContent=reason || '可显式打开；不会自动切换。';
    /** @type {HTMLButtonElement} */(box.querySelector('button')).disabled=!!reason;
  }
}
function publishState() {shell.publish({instance,connected,session,active,revision,unavailable,memoryRevision:memory.revision,memoryState:memory.state,projection:hostProjection,readGapRevision});renderShellStatus();}
function retireLocation() {locateGeneration++;locateController?.abort();locateController=null;if(locationTarget?.loading){locationTarget=null;renderMessages();}}
function returnLatest() {retireLocation();locationTarget=null;followLatest=true;renderMessages();drawer.scrollTop=drawer.scrollHeight;updateReadStatus();}
element('return-latest').addEventListener('click',returnLatest);
element('retry-location').addEventListener('click',()=>{if(locationTarget)void locateReply(/** @type {any} */({...locationTarget,action_id:crypto.randomUUID()}));});
drawer.addEventListener('scroll',()=>{followLatest=drawer.scrollHeight-drawer.scrollTop-drawer.clientHeight<48;updateReadStatus();});
document.addEventListener('visibilitychange',updateReadStatus);
/** Exact target consumer; callers must supply the complete immutable identity. @param {{process_instance_id:string,session_id:string,run_id:string,action_id:string}} target */
async function locateReply(target) {
  if(!target || target.process_instance_id!==instance || typeof target.session_id!=='string' || typeof target.run_id!=='string' || !target.action_id)return {status:'unavailable',reason:'定位身份已失效'};
  if(target.session_id!==session && !await resume(target.session_id))return {status:'blocked',reason:'当前会话的运行尚未收尾'};
  retireLocation();const mine=locateGeneration;const owner=shell.generation;
  locationTarget={...target,loading:true};locatedRecord=null;followLatest=false;
  shell.openPanel('mainbar');updateReadStatus();
  const intent=readingIntent;
  const controller=new AbortController();locateController=controller;
  try {
    const response=await fetch('/api/mainbar/locate?'+new URLSearchParams({process_instance_id:target.process_instance_id,session_id:target.session_id,run_id:target.run_id}),{signal:controller.signal});
    const body=await response.json();
    if(mine!==locateGeneration || owner!==shell.generation || instance!==target.process_instance_id || session!==target.session_id)return {status:'retired'};
    if(!response.ok) {
      const labels=/** @type {Record<string,string>} */({reply_context_expired:'实例已变化，请重新同步后定位。',reply_target_unavailable:'目标记录不可用。',reply_withheld:'正文受保护，暂不可读取。',reply_unavailable:'正文未完整加载，可只读重试。'});
      throw new Error(labels[body.code]||'定位读取失败，可只读重试。');
    }
    if(body.process_instance_id!==instance || body.session_id!==session || body.run_id!==target.run_id || !['chat','aggregation'].includes(body.purpose) || typeof body.item_key!=='string' || body.history_contiguous!==false)throw new Error('定位响应身份无法核验。');
    locatedRecord=body;
    const old=replies.get(target.run_id)||{};
    const recording=['recorded','failed'].includes(old.recording_state)&&body.recording_state==='pending'?old.recording_state:body.recording_state;
    const completeness=/** @type {Record<string,number>} */({full:2,preview:1,unavailable:0});
    const keepUser=(completeness[old.userAvailability]||0)>(completeness[body.user?.availability]||0);
    replies.set(target.run_id,{...old,session_id:session,user:keepUser?old.user:body.user?.availability==='full'?textBlocks(body.user.blocks):body.user?.preview||old.user,userAvailability:keepUser?old.userAvailability:body.user?.availability,reply_disposition:body.reply_disposition,text:body.reply_disposition==='no_reply'?undefined:body.reply_text,skill_notice:body.skill_notice,aggregation:body.aggregation||old.aggregation,recording_state:recording,recording_unverified:false,loading:body.reply_disposition!=='no_reply' && typeof body.reply_text!=='string',purpose:body.purpose});
    locationTarget={...target,loading:false,purpose:body.purpose,reply_disposition:body.reply_disposition};
    renderMessages();
    const record=messageNodes.get('run:'+target.run_id)?.element;
    if(record && intent===readingIntent && shell.visibleMainbar()){drawer.scrollTop+=record.getBoundingClientRect().top-drawer.getBoundingClientRect().top;record.focus({preventScroll:true});}
    return {status:'applied'};
  }catch(failure){
    if(mine!==locateGeneration || controller.signal.aborted)return {status:'retired'};
    locationTarget={...target,loading:false,error:failure instanceof Error?failure.message:'正文未完整加载'};updateReadStatus();return {status:'unavailable'};
  }
}
function renderMessages() {
  const list=element('messages');
  const selection=document.getSelection();
  const selected=selection && !selection.isCollapsed && selection.anchorNode && drawer.contains(selection.anchorNode);
  const follow=followLatest && !locationTarget && !selected;
  const oldTop=drawer.scrollTop;
  /** @type {Map<string,Wire>} */ const items=new Map();
  for(const [index,item] of historyItems.entries()) {
    const key=item.type==='run_pair'?'run:'+item.run_id:item.item_key||'historic:'+index;
    items.set(key,{...item,key});
  }
  for(const [id,reply] of replies) {
    if(reply.session_id!==session)continue;
    const key='run:'+id;const recorded=items.get(key);
    if(recorded){items.set(key,{...recorded,replyMeta:reply});continue;}
    if(locatedRecord?.run_id===id && !locationTarget)continue;
    items.set(key,{...reply,key,run_id:id,type:'projection'});
  }
  if(locationTarget && !items.has('run:'+locationTarget.run_id) && locationTarget.loading)items.set('location-loading',{key:'location-loading',text:'正在读取目标记录…'});
  const temporary=progress.text(session);if(temporary)items.set('temporary',{key:'temporary',text:temporary});
  const liveKeys=new Set(items.keys());
  for(const [key,entry] of messageNodes)if(!liveKeys.has(key)){entry.element.remove();messageNodes.delete(key);}
  let position=0;
  for(const [key,item] of items) {
    const signature=JSON.stringify(item);let entry=messageNodes.get(key);
    if(!entry){entry={element:node('article'),signature:''};entry.element.tabIndex=-1;messageNodes.set(key,entry);}
    const article=entry.element;
    if(entry.signature!==signature) {
      entry.signature=signature;article.replaceChildren();
      if(typeof item.run_id==='string')article.dataset.runId=item.run_id;
      article.dataset.replyComplete=String(item.type==='run_pair'?!!item.assistant&&item.reply_disposition!=='no_reply':item.type==='projection'&&!item.loading&&!item.recording_unverified&&typeof item.text==='string'&&['recorded','failed','pending'].includes(item.recording_state));
      if(item.aggregation)aggregationFacts(article,item.aggregation,memory);
      if(item.type==='run_pair') {
        if(item.user)article.append(node('p',textBlocks(item.user)));
        if(item.assistant)article.append(node('p',textBlocks(item.assistant)));
        if(item.reply_disposition==='no_reply'&&!item.aggregation)article.append(node('small','已结束 · 按要求未回复'));
        if(item.skill_notice)article.append(node('p',item.skill_notice));
        article.append(node('small','已保存'));
      }else if(item.type==='historic_message')article.append(node('p',textBlocks(item.blocks)));
      else {
        if(item.userAvailability==='preview')article.append(node('small','请求预览（未取得完整请求）'));
        if(item.user)article.append(node('p',item.user));
        if(item.text!==undefined&&item.text!==null&&item.reply_disposition!=='no_reply')article.append(node('p',item.text));
        if(item.skill_notice)article.append(node('p',item.skill_notice));
        if(item.reply_disposition==='no_reply'&&!item.aggregation)article.append(node('small','已结束 · 按要求未回复'));
        if(item.loading){article.append(node('p','正文未完整加载'));const retry=node('button','重新加载正文');retry.onclick=()=>void recoverReply(item.run_id);article.append(retry);}
        if(item.outcome && (item.reply_disposition!=='no_reply'||item.outcome!=='completed'))article.append(node('small',outcomeLabel({...item,reply_disposition:undefined})));
        if(item.type==='projection')article.append(node('small',item.recording_unverified?'记录状态待核对':item.recording_state==='recorded'?'已保存':item.recording_state==='failed'?item.reply_disposition==='no_reply'?'本次运行未保存':'回复已收到但未保存':item.recording_state==='pending'?'正在保存':'记录状态未知'));
      }
    }
    article.classList.toggle('located',!!locationTarget&&item.run_id===locationTarget.run_id);
    if(list.children[position]!==article)list.insertBefore(article,list.children[position]||null);
    position++;
  }
  if(follow)drawer.scrollTop=drawer.scrollHeight;else drawer.scrollTop=oldTop;
  updateReadStatus();
}
function resetHistory() {
  sessionGeneration++;
  retireLocation();locationTarget=null;locatedRecord=null;messageNodes.clear();element("messages").replaceChildren();restoreUnread();
  historyRequest++;
  historyItems = [];
  historyCursor = "";
  historyLoaded = false;
  historyLoading = false;
  historyRefresh = false;
  historyPending = false;
}
/** @param {boolean} [older] */
async function loadMessages(older = false) {
  const target = session;
  if (target === null) return;
  if (historyLoading) {
    if (!older) historyRefresh = true;
    return;
  }
  const process = instance;
  const request = ++historyRequest;
  historyLoading = true;
  const button = /** @type {HTMLButtonElement} */ (element("older"));
  button.disabled = true;
  const height = drawer.scrollHeight;
  try {
    const params = {
      session_id: target,
      ...(older && historyCursor ? { cursor: historyCursor } : {}),
    };
    const response = await fetch("/api/mainbar?" + new URLSearchParams(params));
    const page = await response.json();
    if (session !== target || instance !== process || request !== historyRequest) return;
    if (!response.ok) {
      if (response.status === 404) valid = false;
      throw new Error(
        response.status === 404
          ? "会话已失效；草稿已保留，请选择或新建会话。"
          : "对话暂时无法加载；草稿已保留。",
      );
    }
    valid = true;
    const resumePending = historyPending && !historyCursor && !historyItems.some(item => item.type === "historic_message");
    const items = /** @type {Wire[]} */ ([...page.items].reverse());
    for(const item of items)if(item.type==='run_pair'){
      const reply=replies.get(item.run_id);
      if(reply && reply.session_id===target){reply.recording_state='recorded';reply.recording_unverified=false;}
      if(terminalStatus && terminalStatus.run_id===item.run_id && terminalStatus.session_id===target){terminalStatus.recording_state='recorded';terminalStatus.recording_unverified=false;}
    }
    if (!historyLoaded) historyItems = items;
    else {
      const pairs = new Map(
        historyItems
          .filter((item) => item.type === "run_pair")
          .map((item) => [item.run_id, item]),
      );
      for (const item of items)
        if (item.type === "run_pair") pairs.set(item.run_id, item);
      const historic = historyItems.filter(
        (item) => item.type === "historic_message",
      );
      historyItems = [
        ...(older || (resumePending && !historic.length)
          ? items.filter((item) => item.type === "historic_message")
          : []),
        ...historic,
        ...[...pairs.values()].sort(
          (a, b) => a.activity_revision - b.activity_revision,
        ),
      ];
    }
    if (!historyLoaded || older || resumePending)
      historyCursor = page.next_cursor || "";
    historyPending = page.runs_pending;
    historyLoaded = true;
    button.hidden = !historyCursor;
    button.textContent = historyPending
      ? "当前运行保存后继续读取"
      : "更早的消息";
    renderMessages();
    if (older) drawer.scrollTop += drawer.scrollHeight - height;
  } catch (failure) {
    if (request === historyRequest)
      error.textContent =
        failure instanceof Error
          ? failure.message
          : "对话暂时无法加载；草稿已保留。";
  } finally {
    if (request === historyRequest) {
      historyLoading = false;
      button.disabled = historyPending;
      updateSend();
      if (historyRefresh) {
        historyRefresh = false;
        void loadMessages();
      }
    }
  }
}
element("older").addEventListener("click", () => void loadMessages(true));
const stream = new Stream(
  (kind, body, first) => {
    if (kind === "state_patch") {
      if (first && instance !== body.process_instance_id) {
        instance = body.process_instance_id;
        behaviourView?.sync();
        revision = -1;
        active=null;hostProjection=null;
        replies.clear();
        terminalStatus=null;processGap=null;
        progress.clear();
        resetHistory();
        if (body.session_valid) void loadMessages();
        csrf = "";
        void refreshEntry(instance);
      }
      if (body.process_instance_id !== instance) return;
      if (first) {
        connected = true;
        valid = body.session_valid;
        const restore=restoreRunPage?.page===runPage && restoreRunPage?.generation===shell.generation;
        restoreRunPage=null;
        if(restore)shell.restore();
        notices.snapshot();
        updateSend();
        void memory.connected(instance);
        accountingView?.sync(instance);
        connectionsView?.sync(instance);
        databaseView?.sync();
      }
      if (body.state_revision <= revision) {
        // Matching credentials may have arrived while disconnected. Recheck
        // readiness without reapplying an already accepted Host revision.
        if(first && revision>=0 && body.state_revision===revision && csrf)shell.start();
        return;
      }
      notices.snapshot();
      const incoming = body.active_run;
      if (
        incoming?.recording_state === "pending" &&
        ["recorded", "failed"].includes(
          replies.get(incoming.run_id)?.recording_state,
        )
      )
        return;
      revision = body.state_revision;
      connected = true;
      valid = body.session_valid;
      active = body.coordinator_state === "idle" ? null : incoming;
      // Only this accepted Host revision may retain or release the current slot.
      const currentProjection=body.unrecorded_terminal_projection;
      hostProjection=currentProjection?{
        process_instance_id:instance,state_revision:revision,
        run_id:currentProjection.run_id,session_id:currentProjection.session_id,
        purpose:currentProjection.purpose,phase:'finished',outcome:currentProjection.outcome,
        recording_state:currentProjection.recording_state,
        reply_disposition:currentProjection.reply_disposition,aggregation:currentProjection.aggregation,
        gateway:incoming?.run_id===currentProjection.run_id?incoming.gateway:null,
        started_at:incoming?.run_id===currentProjection.run_id?incoming.started_at:null,
      }:null;
      if (!first) accountingView?.sync(instance);
      progress.snapshot(active, body.step);
      notices.settled(
        body.coordinator_state === "idle",
        ![...progress.attempts.values()].some(
          (attempt) => attempt.suppressed && !attempt.terminal,
        ),
      );
      unavailable = body.coordinator_state === "recording_failed";
      if(first && body.coordinator_state==='idle'){
        let recheck=terminalStatus?.session_id===session && terminalStatus?.recording_state==='pending';
        if(terminalStatus?.recording_state==='pending')terminalStatus.recording_unverified=true;
        for(const reply of replies.values())if(reply.recording_state==='pending'){reply.recording_unverified=true;if(reply.session_id===session)recheck=true;}
        if(recheck)void loadMessages();
      }
      const projection = body.unrecorded_terminal_projection;
      if (projection) {
        const old = replies.get(projection.run_id) || {};
        replies.set(projection.run_id, {
          ...old,
          session_id: projection.session_id,
          user: old.user || projection.prompt_preview,
          userAvailability:old.userAvailability||'preview',
          outcome: projection.outcome,
          aggregation: projection.aggregation,
          reply_disposition: projection.reply_disposition || old.reply_disposition,
          recording_state: projection.recording_state,
          recording_unverified:false,
          loading: projection.reply_disposition !== "no_reply" && old.text === undefined,
        });
        terminalStatus={...projection};
        noteReply(projection.run_id,replies.get(projection.run_id)||{});
        if (projection.session_id === session && old.text === undefined && projection.reply_disposition !== "no_reply")
          void recoverReply(projection.run_id);
        if (
          projection.recording_state === "failed" &&
          !alerted.has(projection.run_id)
        ) {
          alerted.add(projection.run_id);
          element("alerts").textContent = projection.reply_disposition === "no_reply" ? "本次运行未保存" : "回复已收到但未保存";
        }
      }
      const stages = /** @type {Record<string,string>} */ ({
        accepted: "已接受",
        running: "运行中",
        recording_pending: "正在保存",
        recording_failed: "保存失败",
      });
      renderBusy(
        active
          ? {
              ...active,
              stage: stages[body.coordinator_state],
              navigation: {
                href: `/runs/${encodeURIComponent(active.run_id)}?filter=${["chat","aggregation"].includes(active.purpose) ? "chat" : "system"}`,
              },
            }
          : null,
      );
      announcer.say(active ? stages[body.coordinator_state] : "就绪");
      if (unavailable) error.textContent = "记录服务不可用；草稿已保留。";
      if (incoming?.recording_state) {
        if(terminalStatus && terminalStatus.run_id===incoming.run_id){terminalStatus.recording_state=incoming.recording_state;terminalStatus.recording_unverified=false;}
        const old = replies.get(incoming.run_id);
        if (old) {old.recording_state = incoming.recording_state;old.recording_unverified=false;}
        if (incoming.recording_state === "recorded") void loadMessages();
      }
      if (!valid && session !== null)
        error.textContent = "会话已失效；草稿已保留，请选择或新建会话。";
      renderMessages();
      updateSend();
      runPage?.sync(active);
      publishState();
      if(csrf)shell.start();
    } else if (kind === "domain_event") {
      if (!progress.receive(body)) return;
      const event = body.payload;
      const envelope = body.envelope;
      if(event.name==='run.finished')terminalStatus={...event,run_id:envelope.run_id,session_id:envelope.session_id,recording_state:replies.get(envelope.run_id)?.recording_state||'pending'};
      notices.settled(
        event.name === "run.finished",
        ![...progress.attempts.values()].some(
          (attempt) => attempt.suppressed && !attempt.terminal,
        ),
      );
      if (event.name === "run.finished" && envelope.session_id === session) {
        const old = replies.get(envelope.run_id) || {};
        replies.set(envelope.run_id, {
          ...old,
          session_id: envelope.session_id,
          outcome: event.outcome,
          aggregation: event.aggregation,
          skill_notice: event.skill_notice,
          reply_disposition: event.reply_disposition,
          text: event.reply_disposition === "no_reply" ? undefined : textBlocks(event.reply?.blocks) || event.error || "运行已结束",
          recording_state: old.recording_state || "pending",
          loading: false,
        });
        noteReply(envelope.run_id,replies.get(envelope.run_id)||{});
        renderMessages();
        void loadMessages();
      }
      renderMessages();
      runPage?.update();
      if (event.name === "run.finished") {
        void runPage?.loadEvidence();
        void connectionsView?.refresh();
      }
    } else if (kind === "transport_notice") {
      if(body.code==='deltas_dropped' || body.code==='replay_gap'&&body.current_run_state==='unrecoverable')processGap={run_id:active?.run_id,message:body.code==='deltas_dropped'?'部分过程增量未收到':'当前运行的过程无法完整恢复'};
      progress.interrupt();
      notices.receive(body);
      renderMessages();
      if(body.code==='replay_gap'||body.code==='deltas_dropped'){readGapRevision++;publishState();}
    } else if (kind === "memory_patch") {
      memory.patch(body);
      publishState();
      databaseView?.invalidate("memory");
    } else if (kind === "protection_patch") databaseView?.invalidate("protection");
  },
  () => {
    connected = false;
    progress.interrupt();
    notices.disconnected();
    memory.disconnected();
    accountingView?.disconnect();
    databaseView?.disconnect();
    behaviourView?.disconnect();
    renderMessages();
    updateSend();
    publishState();
  },
);

/** @param {string} expected */
async function refreshEntry(expected) {
  try {
    const response = await fetch("/api/entry");
    const entry = await response.json();
    if (instance !== expected) return;
    if (!response.ok || entry.instance_id !== expected)
      throw new Error("实例已改变");
    csrf = entry.csrf_token;
  } catch {
    if (instance === expected)
      error.textContent = "入口暂不可用；请刷新页面，草稿仍保留。";
  }
  updateSend();
  if(csrf && connected && revision>=0)shell.start();
}

async function send() {
  if (!canSend()) return;
  returnLatest();
  const target = session;
  const draft = input.value;
  sending = true;
  updateSend();
  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-agent-alfred-csrf": csrf,
      },
      body: JSON.stringify({ message: draft, session_id: target }),
    });
    const result = await response.json();
    if (response.status === 409) {
      renderBusy(result.active_run_summary || null);
      if (!result.active_run_summary)
        error.textContent = "当前正忙；草稿已保留。";
      return;
    }
    if (response.status === 503) {
      unavailable = true;
      throw new Error("记录服务不可用；草稿已保留。");
    }
    if (response.status === 404) {
      valid = false;
      throw new Error("会话已失效；草稿已保留，请选择或新建会话。");
    }
    if (response.status !== 202) throw new Error("消息未获准；草稿已保留。");
    const old = replies.get(result.run_id) || {};
    replies.set(result.run_id, { ...old, session_id: target, user: draft, userAvailability:'full' });
    if (session === target && input.value === draft) {
      input.value = "";
      storage.set(`alfred.draft:${target}`, "");
    }
    renderMessages();
  } catch (failure) {
    error.textContent =
      failure instanceof Error && !["TypeError", "SyntaxError"].includes(failure.name) ? failure.message : "受理结果未确认；草稿已保留，请查看已有运行核对，不会自动重投。";
  } finally {
    sending = false;
    updateSend();
  }
}
element("compose").addEventListener("submit", (event) => {
  event.preventDefault();
  void send();
});
input.addEventListener("keydown", (event) => {
  if (
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.isComposing &&
    event.keyCode !== 229
  ) {
    event.preventDefault();
    void send();
  }
});

function restoreDraft() {
  input.disabled = session === null;
  input.value =
    session === null
      ? ""
      : storage.get(`alfred.draft:${session}`) || "";
  element("session-name").textContent =
    session === null ? "尚未选择会话" : "会话";
}
/** Each mount receives a distinct root; late page responses cannot target its successor. */
/** @param {URL} url */
function mountPage(url) {
  const path=url.pathname;
  const heading=node("h1", pageDefinition(url)?.title || "页面未找到");
  heading.tabIndex=-1;
  const root=node("div");root.className="page-body";
  element("page").replaceChildren(heading,root);
  runPage=null;connectionsView=null;databaseView=null;behaviourView=null;accountingView=null;
  let owner=/** @type {any} */(null);
  if(path==='/overview')owner=overviewPage(root,{url,replaceSource:(target)=>shell.replaceSource(target)});
  else if(path==='/behaviour')owner=behaviourView=behaviourPage(root,()=>csrf,()=>({instance,active,connected,unavailable,projection:[...replies.values()].find(r=>r.aggregation&&r.recording_state!=='recorded')}),memory,()=>instance);
  else if(path==='/tools')owner=accountingView=toolsPage(root,()=>csrf);
  else if(path==='/ops')owner=accountingView=accountingPage(root,()=>csrf);
  else if(path==='/models')owner=modelsPage(root,()=>csrf);
  else if(path==='/connections')owner=connectionsView=connectionsPage(root,()=>csrf);
  else if(path==='/memory')owner=memoryPage(root,memory,{csrf:()=>csrf,session:()=>session,receipts,replaceSource:(target)=>shell.replaceSource(target)});
  else if(path==='/database')owner=databaseView=databasePage(root,{csrf:()=>csrf,instance:()=>instance,connected:()=>connected});
  else if(path==='/runs'||path.startsWith('/runs/'))owner=runPage=runsPage(root,progress,dashboard,memory,()=>csrf);
  else if(path==='/inbox'||path==='/')owner=inbox(root,dashboard);
  else root.append(node('p','此地址不可用。'));
  if(connected){runPage?.sync(active);connectionsView?.sync(instance);}
  return {
    getLeaveState:()=>owner?.getLeaveState?.()||{dirty:false},
    setVisible:(/** @type {boolean} */ visible)=>owner?.setVisible?.(visible),
    captureSource:()=>owner?.captureSource?.()||{},
    restoreSource:(/** @type {any} */ source)=>owner?.restoreSource?.(source),
    dispose(){if(restoreRunPage?.page===owner)restoreRunPage=null;retireLocation();receipts.detach();if(owner?.dispose)owner.dispose();else owner?.close?.();root.remove();},
  };
}
const shell=createShell({mount:mountPage,storage,onVisibility:()=>queueMicrotask(updateReadStatus)});
memory.watch(publishState);
/** Shared page ports preserve the one MainBar/Stream owner. */
export const dashboard = {
  navigate:shell.navigate,openPanel:shell.openPanel,closePanel:shell.closePanel,
  subscribeState:shell.subscribeState,selectSession:resume,locateReply,returnLatest,
  sessionActionState,continueSession,createSession:()=>createSession(true),
  subscribeSessionActions(/** @type {()=>void} */ listener) {sessionActionListeners.add(listener);listener();return ()=>sessionActionListeners.delete(listener);},
  runtime:()=>({instance,connected,session,active,revision,unavailable}),
};
/** @param {string} target */
async function resume(target) {
  return (await continueSession(target)).status === 'applied';
}
/** @param {string} target */
async function continueSession(target) {
  const state = sessionActionState('continue',target);
  if (!state.allowed) {error.textContent=state.reason;return {status:'blocked',reason:state.reason};}
  if (target !== session) {
    retireLocation();
    session = target;
    storage.set("alfred.session", target);
    resetHistory();
    valid = false;
    connected = false;
    revision = -1;
    restoreDraft();
    progress.interrupt();
    memory.disconnected();
    accountingView?.disconnect();
    stream.connect(session);
  }
  for(const [key,receipt] of createdSessions)if(receipt.session_id===target)createdSessions.delete(key);
  renderShellStatus();
  shell.openPanel('mainbar');
  updateSend();
  void loadMessages();
  return {status:'applied',session_id:target};
}
input.addEventListener("input", () => {
  if (session !== null) storage.set(`alfred.draft:${session}`, input.value);
  updateSend();
});
window.addEventListener("offline", () => {
  connected = false;
  stream.source?.close();
  progress.interrupt();
  memory.disconnected();
  accountingView?.disconnect();
  databaseView?.disconnect();
  behaviourView?.disconnect();
  updateSend();
  publishState();
});
window.addEventListener("online", () => stream.connect(session));
window.addEventListener("pagehide", () => databaseView?.suspend());
window.addEventListener("pageshow", (event) => {
  if (event.persisted) databaseView?.restoredFromCache();
  if (event.persisted && runPage) {
    restoreRunPage={page:runPage,generation:shell.generation};
    connected = false;
    stream.source?.close();
    memory.disconnected();
    stream.connect(session);
  }
});
/** @param {boolean} [reveal] */
async function createSession(reveal = false) {
  const state = sessionActionState('create');
  if (!state.allowed) {error.textContent=state.reason;return {status:'blocked',reason:state.reason};}
  creating=true;
  const intent=readingIntent;const pageOwner=shell.generation;
  const owner={instance,session,generation:sessionGeneration};
  updateSend();
  try {
    const response = await fetch("/api/sessions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-agent-alfred-csrf": csrf,
      },
      body: "{}",
    });
    const result = await response.json();
    if (!response.ok || typeof result.session_id !== "string") {
      const reasons=/** @type {Record<string,string>} */({mutation_in_flight:'当前运行或保存操作尚未收尾，请稍后重试。',recording_unavailable:'记录服务不可用，暂不能新建会话。',admission_failed:'运行受理不可用，暂不能新建会话。'});
      throw new Error(reasons[result.code] || "会话未能创建，请稍后重试。");
    }
    createdSessionRevision++;
    // Creation is committed on the server. Selecting it is still owned by the
    // original user intent and must pass the current guard, including other tabs.
    const reason=owner.instance!==instance ? '创建时的进程已变化，请重新核验。'
      : owner.session!==session || owner.generation!==sessionGeneration ? '当前会话选择已变化，保留当前会话。'
      : !connected || revision<0 ? '连接尚未同步，保留当前会话。'
      : createSessionReason()
        || (pageOwner!==shell.generation || intent!==readingIntent ? '已有新的页面或阅读意图，保留当前会话和草稿。' : '');
    if(reason) {
      createdSessions.set(JSON.stringify([owner.instance,result.session_id]),{session_id:result.session_id,instance:owner.instance,reason});
      error.textContent='';
      return {status:'created',session_id:result.session_id,reason:'会话已创建，尚未切换：'+reason};
    }
    retireLocation();
    session = result.session_id;
    storage.set("alfred.session", /** @type {string} */ (session));
    error.textContent = "";
    restoreDraft();
    resetHistory();
    connected = false;
    revision = -1;
    memory.disconnected();
    accountingView?.disconnect();
    stream.connect(session);
    if(pageOwner===shell.generation && readingIntent===intent && (reveal || shell.visibleMainbar()))shell.openPanel("mainbar");
    void loadMessages();
    return {status:'applied',session_id:session};
  } catch (failure) {
    error.textContent =
      failure instanceof Error ? failure.message : "连接不可用";
    return {status:'failed',reason:error.textContent};
  } finally {
    creating=false;
    updateSend();
  }
}
element("new-session").addEventListener("click", () => void createSession());
restoreDraft();
restoreUnread();
try {
  const response = await fetch("/api/entry");
  const entry = await response.json();
  if (!response.ok) throw new Error("连接不可用");
  csrf = entry.csrf_token;
  updateSend();
  instance = entry.instance_id;
  stream.connect(session);
  await loadMessages();
} catch {
  error.textContent = "连接不可用；草稿仍保留在本标签页。";
}
