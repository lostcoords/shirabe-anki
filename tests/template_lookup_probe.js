(() => {
  window.__shirabeLookupTiming?.dispose();
  if (!window.__shirabe?.native || !window.__shirabe.useCollectionStates || typeof window.pycmd !== 'function') {
    throw new Error('The timing fixture requires the native collection-state bridge');
  }
  const originalBridge = window.pycmd;
  const qa = document.getElementById('qa');
  let active = null;
  let frame = null;
  const probe = window.__shirabeLookupTiming = { result: null, arm, dispose };
  const popups = () => [...document.querySelectorAll('.sb-host')].flatMap(host =>
    [...(host.shadowRoot?.querySelectorAll('.sb-popup.sb-visible') || [])]);

  function visible(element) {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    if (!rect.width || !rect.height || rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth) return false;
    for (let node = element; node; node = node.parentElement || node.getRootNode().host) {
      const style = getComputedStyle(node);
      if (node.hidden || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || 1) <= 0) return false;
    }
    return true;
  }

  function definition() {
    for (const popup of popups()) {
      const headword = popup.querySelector('.sb-headword');
      const gloss = [...popup.querySelectorAll('.sb-gloss-row')].find(row => row.textContent.trim() === active.gloss && visible(row));
      if (visible(popup) && visible(headword) && headword.textContent.includes(active.headword) && gloss) {
        return { headword: headword.textContent.trim(), gloss: gloss.textContent.trim() };
      }
    }
    return null;
  }

  function observe() {
    if (!active || active.started == null) return;
    const found = definition();
    if (found) {
      probe.result = { elapsedMs: performance.now() - active.started, ...found,
        event: active.event, collectionStates: active.states, cardId: window.__shirabe.cardId };
      active = null;
    } else if (performance.now() - active.started > 10000) {
      probe.result = { error: 'Expected visible definition was not rendered within 10 seconds' };
      active = null;
    } else frame = requestAnimationFrame(observe);
  }

  function clicked(event) {
    if (!active || active.started != null || !qa.contains(event.target)) return;
    active.started = performance.now();
    active.event = { type: event.type, trusted: event.isTrusted };
    frame = requestAnimationFrame(observe);
  }

  function arm(headword, gloss) {
    if (popups().length) throw new Error('Dismiss the current popup before arming another lookup');
    if (frame != null) cancelAnimationFrame(frame);
    probe.result = null;
    active = { headword, gloss, states: [], started: null };
    return true;
  }

  function bridge(command, callback, ...rest) {
    if (!active || active.started == null || !command.startsWith('shirabe:states:')) {
      return originalBridge.call(this, command, callback, ...rest);
    }
    const state = { completed: false };
    const started = performance.now();
    active.states.push(state);
    return originalBridge.call(this, command, answer => {
      state.elapsedMs = performance.now() - started;
      try { state.states = JSON.parse(answer); state.completed = true; }
      catch { state.error = 'Native collection-state response was not JSON'; }
      callback(answer);
    }, ...rest);
  }

  function dispose() {
    active = null;
    if (frame != null) cancelAnimationFrame(frame);
    window.removeEventListener('click', clicked, true);
    if (window.pycmd === bridge) window.pycmd = originalBridge;
  }

  window.addEventListener('click', clicked, true);
  window.pycmd = bridge;
  return true;
})()
