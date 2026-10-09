// Observation-only instrumentation. No input dispatch, focus, or product mutation.
(() => {
  const events = ['compositionstart', 'compositionupdate', 'compositionend', 'keydown', 'keyup', 'beforeinput', 'input', 'focusin', 'focusout', 'click'];
  let seq = 0;
  for (const type of events) {
    document.addEventListener(type, event => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const data = {seq: ++seq, at: new Date().toISOString(), type, trusted: event.isTrusted,
        target: target.id || target.getAttribute('aria-label') || target.tagName,
        key: event.key, isComposing: event.isComposing, data: event.data, inputType: event.inputType,
        value: target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement ? target.value : null,
        active: document.activeElement?.id, route: location.pathname};
      fetch('/__native_evidence__', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data), keepalive:true}).catch(console.error);
      console.log('NATIVE_EVIDENCE', JSON.stringify(data));
    }, true);
  }
})();
