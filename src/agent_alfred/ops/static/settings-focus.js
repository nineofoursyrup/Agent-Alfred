/** @typedef {{key:string,selection:[number|null,number|null]|null}} Focus */

/** Preserve one settings control across redraw/temporary disable, until new intent.
 * @param {HTMLElement} root
 */
export function settingsFocus(root) {
  /** @type {Focus|null} */ let pending=null;
  let restoring=false;
  const events=['focusin','pointerdown','keydown','wheel','touchstart'];
  const retire=()=>{if(!restoring)pending=null;};
  for(const kind of events)document.addEventListener(kind,retire,{capture:true,passive:true});
  return {
    capture() {
      const active=document.activeElement;
      if(active instanceof HTMLElement && root.contains(active) && active.dataset.focusKey) {
        pending={key:active.dataset.focusKey,selection:active instanceof HTMLInputElement?[active.selectionStart,active.selectionEnd]:null};
      } else if(active!==document.body)pending=null;
      return pending;
    },
    /** @param {Focus|null} request */
    restore(request) {
      if(!request || request!==pending || !root.isConnected || document.activeElement!==document.body)return;
      const target=[...root.querySelectorAll('[data-focus-key]')].find(element=>element instanceof HTMLElement && element.dataset.focusKey===request.key);
      if(!(target instanceof HTMLElement) || root.closest('[inert]') || !target.getClientRects().length){pending=null;return;}
      // Native disabled buttons cannot receive focus. Keep this exact identity
      // only while there has been no new keyboard, pointer or focus intent.
      if(target.matches(':disabled'))return;
      pending=null;restoring=true;
      try {
        target.focus({preventScroll:true});
        if(request.selection && target instanceof HTMLInputElement)target.setSelectionRange(...request.selection);
      } finally {restoring=false;}
    },
    close(){pending=null;for(const kind of events)document.removeEventListener(kind,retire,true);},
  };
}
