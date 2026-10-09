import {node} from './dom.js';

/** The only source of route, navigation and title identities. */
export const PAGES = [
  {path:'/overview', title:'总览', english:'Overview', group:'工作区'},
  {path:'/inbox', title:'收件箱', english:'Inbox', group:'工作区'},
  {path:'/runs', title:'运行', english:'Runs', group:'工作区'},
  {path:'/memory', title:'记忆', english:'Memory', group:'工作区'},
  {path:'/models', title:'模型', english:'Models', group:'Agent 配置'},
  {path:'/connections', title:'连接', english:'Connections', group:'Agent 配置'},
  {path:'/behaviour', title:'行为', english:'Behaviour', group:'Agent 配置'},
  {path:'/tools', title:'工具', english:'Tools', group:'Agent 配置'},
  {path:'/ops', title:'用量账本', english:'Ops', group:'诊断'},
  {path:'/database', title:'数据库', english:'Database', group:'诊断'},
];
/** @typedef {{dirty:boolean, summary?:string, pending?:boolean}} LeaveState */
/** @typedef {{getLeaveState?:()=>LeaveState, setVisible?:(visible:boolean)=>void, captureSource?:()=>Object, restoreSource?:(source:any)=>void, dispose?:()=>void}} PageController */
/** @param {URL} url */
export function pageDefinition(url) {
  return PAGES.find(page => page.path === url.pathname || (page.path === '/runs' && url.pathname.startsWith('/runs/')) || (page.path === '/inbox' && url.pathname === '/'));
}

/** Storage failures degrade to this document's memory; never lose typed text. */
export class TabStorage {
  /** @param {()=>void} failed */
  constructor(failed) { this.failed=failed; /** @type {Map<string,string>} */ this.memory=new Map(); }
  /** @param {string} key */
  get(key) {
    if (this.memory.has(key)) return this.memory.get(key) ?? null;
    try { return sessionStorage.getItem(key); } catch { this.failed(); return null; }
  }
  /** @param {string} key @param {string} value */
  set(key,value) { this.memory.set(key,value); try { sessionStorage.setItem(key,value); } catch { this.failed(); } }
}

/** Explicitly registered editable inputs, owned by their page, not discovered by the shell. */
export class Drafts {
  constructor() { /** @type {Map<HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement,string>} */ this.inputs=new Map(); }
  /** @param {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement} input */
  value(input) { return input instanceof HTMLInputElement && input.type==='checkbox' ? String(input.checked) : input.value; }
  /** @param {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement} input */
  track(input) { this.inputs.set(input,this.value(input)); return input; }
  /** @param {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement} input @param {string} [value] */
  saved(input,value=this.value(input)) { this.inputs.set(input,value); }
  dirty() { return [...this.inputs].some(([input,value])=>input.isConnected && this.value(input)!==value); }
  /** @param {string} summary @returns {LeaveState} */
  leave(summary) {return {dirty:this.dirty(),summary};}
}

/** @param {LeaveState} state @returns {Promise<boolean>} */
export function confirmLeave(state) {
  if (!state.dirty) return Promise.resolve(true);
  const previous=document.activeElement;
  const dialog=node('dialog'); dialog.setAttribute('aria-labelledby','leave-title');
  const title=node('h2','离开当前页面？'); title.id='leave-title';
  const stay=node('button','留在此页'); const leave=node('button','放弃并离开');
  dialog.append(title,node('p',state.summary || '当前页面有未提交输入。'),node('p','仅放弃未提交输入；已提交动作仍由原服务处理，不会撤销或自动重发。'),stay,leave);
  document.body.append(dialog); dialog.showModal(); stay.focus();
  return new Promise(resolve=>{
    /** @param {boolean} accepted */
    function finish(accepted) {dialog.close();dialog.remove();if (!accepted && previous instanceof HTMLElement && previous.isConnected) previous.focus({preventScroll:true});resolve(accepted);}
    stay.addEventListener('click',()=>finish(false),{once:true});
    leave.addEventListener('click',()=>finish(true),{once:true});
    dialog.addEventListener('cancel',event=>{event.preventDefault();finish(false);},{once:true});
  });
}

/** @param {{mount:(url:URL)=>PageController, storage:TabStorage, onVisibility?:(visible:boolean)=>void, onNavigate?:()=>void}} options */
export function createShell(options) {
  const page=/** @type {HTMLElement} */(document.getElementById('page'));
  const rail=/** @type {HTMLElement} */(document.querySelector('.site-header'));
  const mainbar=/** @type {HTMLElement} */(document.getElementById('mainbar'));
  const toolbar=/** @type {HTMLElement} */(document.getElementById('shell-toolbar'));
  const nav=/** @type {HTMLElement} */(rail.querySelector('nav'));
  const narrow=matchMedia('(max-width:1099px)');
  const documentId=crypto.randomUUID();
  let epoch=0;
  let wideOpen=options.storage.get('alfred.shell.wideOpen')!=='false';
  let temporaryWide=false;
  /** @type {'navigation'|'mainbar'|null} */ let panel=null;
  /** @type {HTMLElement|null} */ let trigger=null;
  /** @type {PageController|null} */ let controller=null;
  let currentUrl=new URL(location.href);
  let current={version:1,documentId,index:0,epoch,panel:/** @type {string|null} */(null),url:currentUrl.href,source:/** @type {any} */(null)};
  /** @type {null|(()=>void)} */ let restoreHistory=null;
  let navigating=false;
  // First readiness belongs to this document, not to a route or connection.
  let started=false;
  let mounted=false;
  let generation=0;
  // Navigation/restore intent is distinct from the first automatic mount.
  let navigationGeneration=0;
  let automaticTitleFocus=false;
  let automaticMainbarFocus=false;
  /** @type {MutationObserver|null} */ let scrollObserver=null;
  let scrollFrame=0;
  function stopScrollRestore(){scrollObserver?.disconnect();scrollObserver=null;cancelAnimationFrame(scrollFrame);scrollFrame=0;}
  /** Restore after async page content grows, but retire on a newer user intent. @param {number} top */
  function restoreScroll(top){
    stopScrollRestore();
    if(!top){page.scrollTop=0;return;}
    function apply(){
      scrollFrame=0;page.scrollTop=top;
      if(Math.abs(page.scrollTop-top)<1)stopScrollRestore();
    }
    scrollObserver=new MutationObserver(()=>{if(!scrollFrame)scrollFrame=requestAnimationFrame(apply);});
    scrollObserver.observe(page,{subtree:true,childList:true,attributes:true});
    apply();
  }
  for(const kind of ['wheel','touchstart','pointerdown','keydown'])page.addEventListener(kind,stopScrollRestore,{passive:true});
  /** @type {Set<(event:any)=>void>} */ const subscribers=new Set();
  let state=/** @type {any} */({});
  function publish() {for(const callback of subscribers)callback(state);}
  function storeCurrent() {history.replaceState({...history.state,alfredShell:current},'',currentUrl.href);}
  function capture() {if (controller) current.source={scrollTop:page.scrollTop,...controller.captureSource?.()};storeCurrent();}
  page.addEventListener('scroll',()=>{if(!scrollObserver&&!navigating&&!restoreHistory&&currentUrl.href===location.href)capture();});
  function activeNav() {
    const def=pageDefinition(currentUrl);
    for(const link of nav.querySelectorAll('a')) {
      if(link.getAttribute('href')===def?.path)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
    }
  }
  let lastGroup='';
  for(const definition of PAGES) {
    if(definition.group!==lastGroup){nav.append(node('p',definition.group));lastGroup=definition.group;}
    const link=node('a',definition.title);link.href=definition.path;link.title=definition.english;nav.append(link);
  }
  function titleFocus() {page.querySelector('h1')?.focus({preventScroll:true});}
  function visibleMainbar() {return narrow.matches ? panel==='mainbar' : wideOpen || temporaryWide;}
  function present() {
    const modal=narrow.matches && panel!==null;
    const chat=visibleMainbar();
    document.body.dataset.layout=narrow.matches?'narrow':'wide';
    document.body.dataset.panel=panel || '';
    document.body.classList.toggle('chat-open',chat);
    page.inert=modal;
    page.setAttribute('aria-hidden',String(modal));
    rail.inert=narrow.matches && panel!=='navigation';
    mainbar.inert=!chat;
    rail.hidden=narrow.matches && panel!=='navigation';
    mainbar.hidden=!chat;
    toolbar.hidden=false;
    toolbar.inert=modal;
    toolbar.setAttribute('aria-hidden',String(modal));
    const status=document.getElementById('shell-status');
    if(status){if(modal && panel==='navigation')rail.insertBefore(status,nav);else if(modal && panel==='mainbar')mainbar.insertBefore(status,mainbar.querySelector('#connection'));else toolbar.append(status);}
    for(const box of [rail,mainbar]) {
      if(modal && ((box===rail && panel==='navigation') || (box===mainbar && panel==='mainbar'))) {box.setAttribute('role','dialog');box.setAttribute('aria-modal','true');}
      else {box.removeAttribute('role');box.removeAttribute('aria-modal');}
    }
    const shade=/** @type {HTMLElement} */(document.getElementById('panel-shade'));shade.hidden=!(narrow.matches && panel==='navigation');
    for(const button of document.querySelectorAll('[data-open-panel]')) button.setAttribute('aria-expanded',String(button.getAttribute('data-open-panel')==='mainbar'?chat:panel==='navigation'));
    controller?.setVisible?.(!modal);
    options.onVisibility?.(chat);
  }
  /** @param {'navigation'|'mainbar'} kind @param {HTMLElement|null} [origin] */
  function openPanel(kind,origin=null) {
    if(origin)trigger=origin;
    if(!narrow.matches){if(kind==='mainbar'){wideOpen=true;temporaryWide=false;options.storage.set('alfred.shell.wideOpen','true');}present();}
    else {
      if(!panel){capture();current={...current,index:current.index+1,panel:kind,epoch};history.pushState({alfredShell:current},'',currentUrl.href);}
      else {current={...current,panel:kind};storeCurrent();}
      panel=kind;present();
    }
    if(kind==='navigation') /** @type {HTMLElement|null} */(nav.querySelector('[aria-current]')||nav.querySelector('a'))?.focus();
    else {
      automaticMainbarFocus=true;
      try {
        if(narrow.matches) document.getElementById('mainbar-title')?.focus({preventScroll:true});
        else {const input=/** @type {HTMLTextAreaElement|null} */(document.getElementById('message'));if(input&&!input.disabled)input.focus({preventScroll:true});else document.getElementById('mainbar-title')?.focus({preventScroll:true});}
      } finally {automaticMainbarFocus=false;}
    }
  }
  function returnFocus() {if(trigger?.isConnected && trigger.getClientRects().length)trigger.focus({preventScroll:true});else titleFocus();}
  function closePanel() {
    if(narrow.matches){if(panel)history.back();}
    else {wideOpen=false;temporaryWide=false;options.storage.set('alfred.shell.wideOpen','false');present();returnFocus();}
  }
  /** @param {boolean} [initial] */
  function mount(initial=false) {
    if(!initial){navigationGeneration++;options.onNavigate?.();}
    if(!started){activeNav();present();return;}
    const preserveMainbarFocus=!mounted && mainbar.contains(document.activeElement);
    stopScrollRestore();
    controller?.dispose?.();generation++;
    controller=options.mount(currentUrl);mounted=true;
    activeNav();present();
    if(current.source){controller.restoreSource?.(current.source);restoreScroll(current.source.scrollTop||0);}else page.scrollTop=0;
    if(!preserveMainbarFocus){
      automaticTitleFocus=initial;
      try{titleFocus();}finally{automaticTitleFocus=false;}
    }
  }
  /** @param {string|URL} target @param {{replace?:boolean,source?:Object,intent?:string}} [intent] */
  async function navigate(target,intent={}) {
    let url;try{url=new URL(target,location.href);}catch{return 'unavailable';}
    if(url.origin!==location.origin || !pageDefinition(url))return 'unavailable';
    if(url.href===currentUrl.href){if(panel)closePanel();return 'unchanged';}
    if(navigating)return 'unavailable';navigating=true;
    try {
      if(!await confirmLeave(controller?.getLeaveState?.()||{dirty:false}))return 'cancelled';
      capture();
      if(panel){await new Promise(resolve=>{restoreHistory=()=>resolve(undefined);history.back();});}
      currentUrl=url;panel=null;
      current={version:1,documentId,index:current.index+(intent.replace?0:1),epoch,panel:null,url:url.href,source:intent.source||null};
      if(intent.replace)storeCurrent();else history.pushState({alfredShell:current},'',url.href);
      mount();return 'applied';
    }finally{navigating=false;}
  }
  window.addEventListener('popstate',async event=>{
    const next=event.state?.alfredShell;
    if(restoreHistory){const done=restoreHistory;restoreHistory=null;if(next){current=next;currentUrl=new URL(current.url);panel=null;present();}done();return;}
    if(!next || next.documentId!==documentId){
      // Browser history from an earlier document has no reusable panel ownership.
      const url=new URL(location.href);
      if(!pageDefinition(url))return;
      if(navigating)return;navigating=true;
      const old=current;const delta=next?next.index-current.index:-1;
      try {
        if(!await confirmLeave(controller?.getLeaveState?.()||{dirty:false})){
          await new Promise(resolve=>{restoreHistory=()=>{current=old;currentUrl=new URL(old.url);panel=null;present();resolve(undefined);};history.go(-delta);});return;
        }
        currentUrl=url;panel=null;current={...current,index:next?.index??current.index-1,url:url.href,panel:null,source:next?.source||null,epoch};storeCurrent();mount();return;
      }finally{navigating=false;}
    }
    const delta=next.index-current.index;
    if(next.url===currentUrl.href){
      if(next.panel && next.epoch!==epoch){history.go(delta<0?-1:1);return;}
      current=next;panel=narrow.matches?next.panel:null;present();if(panel==='mainbar')document.getElementById('mainbar-title')?.focus({preventScroll:true});else if(panel==='navigation')/** @type {HTMLElement|null} */(nav.querySelector('[aria-current]'))?.focus();else returnFocus();return;
    }
    if(navigating)return;navigating=true;
    const old=current;
    try {
      if(!await confirmLeave(controller?.getLeaveState?.()||{dirty:false})) {
        await new Promise(resolve=>{restoreHistory=()=>{current=old;panel=narrow.matches?/** @type {'navigation'|'mainbar'|null} */(old.panel):null;present();resolve(undefined);};history.go(-delta);});
        return;
      }
      current=next;currentUrl=new URL(next.url);panel=null;mount();
    }finally{navigating=false;}
  });
  document.addEventListener('click',event=>{
    if(event.defaultPrevented || event.button!==0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)return;
    const link=event.target instanceof Element?event.target.closest('a'):null;
    if(!link || link.target || link.hasAttribute('download') || link.origin!==location.origin || !pageDefinition(new URL(link.href)))return;
    event.preventDefault();void navigate(link.href);
  });
  for(const button of document.querySelectorAll('[data-open-panel]'))button.addEventListener('click',()=>openPanel(/** @type {'navigation'|'mainbar'} */(button.getAttribute('data-open-panel')),/** @type {HTMLElement} */(button)));
  for(const button of document.querySelectorAll('[data-close-panel]'))button.addEventListener('click',closePanel);
  document.getElementById('panel-shade')?.addEventListener('click',closePanel);
  document.addEventListener('keydown',event=>{
    if(event.defaultPrevented || event.isComposing || document.querySelector('dialog[open]'))return;
    if(event.key==='Escape' && (panel || (!narrow.matches && visibleMainbar() && mainbar.contains(document.activeElement)))){event.preventDefault();closePanel();}
    if(event.key==='Tab' && narrow.matches && panel){
      const box=panel==='navigation'?rail:mainbar;
      const controls=[...box.querySelectorAll('a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,[tabindex="0"]')].filter(item=>item instanceof HTMLElement && item.getClientRects().length);
      const first=/** @type {HTMLElement|undefined} */(controls[0]);const last=/** @type {HTMLElement|undefined} */(controls.at(-1));
      if(event.shiftKey && (document.activeElement===first || !controls.includes(/** @type {Element} */(document.activeElement)))){event.preventDefault();last?.focus();}else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first?.focus();}
    }
  });
  narrow.addEventListener('change',()=>{
    const focused=document.activeElement;const editing=focused instanceof HTMLInputElement || focused instanceof HTMLTextAreaElement || focused instanceof HTMLSelectElement;
    const editingChat=editing && mainbar.contains(focused);
    epoch++;
    const hadPanel=!!panel;panel=null;
    if(!narrow.matches)temporaryWide=editingChat;
    function finishLayout() {
      if(narrow.matches && editingChat){panel='mainbar';current={...current,index:current.index+1,panel,epoch};history.pushState({alfredShell:current},'',currentUrl.href);}
      present();
      if(focused instanceof HTMLElement && focused.getClientRects().length && !focused.closest('[inert]'))focused.focus({preventScroll:true});else titleFocus();
    }
    if(hadPanel){restoreHistory=finishLayout;history.back();}else finishLayout();
  });
  window.addEventListener('beforeunload',event=>{if(controller?.getLeaveState?.().dirty){event.preventDefault();event.returnValue='';}});
  // Reload never reopens an old narrow presentation state.
  const prior=history.state?.alfredShell;
  current.index=prior?.index||0;
  if(prior?.version===1 && prior.panel && prior.url===location.href) {
    restoreHistory=()=>{current={...current,documentId,epoch,panel:null};storeCurrent();present();};
    history.back();
  } else storeCurrent();
  activeNav();present();
  return {
    start(){started=true;if(!mounted&&!navigating)mount(true);},restore(){capture();mount();},navigate,openPanel,closePanel,visibleMainbar,
    get generation(){return generation;},
    get navigationGeneration(){return navigationGeneration;},
    get automaticTitleFocus(){return automaticTitleFocus;},
    get automaticMainbarFocus(){return automaticMainbarFocus;},
    /** @param {any} value */ publish(value){state=Object.freeze({...value});publish();},
    /** @param {(state:any)=>void} callback */ subscribeState(callback){subscribers.add(callback);callback(state);return ()=>subscribers.delete(callback);},
    /** Page-local query changes still share this history identity. @param {string|URL} target */
    replaceSource(target){currentUrl=new URL(target,location.href);current={...current,url:currentUrl.href};storeCurrent();},
  };
}
