/* Injected by Surface into app/html panels. Presentation state belongs to the hub. */
(() => {
  if (!window.surface || window.surface.presentation) return;
  let adapter, state, online = false, registered = false, bar, previous, next, label, start;
  const editable = target => target instanceof Element && !!target.closest('input,textarea,select,button,a,[contenteditable=true],video,audio');
  const request = (command, index) => {
    if (!adapter || !online || !state || state.deck !== adapter.id) return;
    window.surface.emit('__presentation-command', {deck:adapter.id,command,index,request:crypto.randomUUID()});
  };
  function render() {
    if (!bar) return;
    const ready = online && state?.deck === adapter.id;
    previous.disabled = !ready || state.index === 0;
    next.disabled = !ready || state.index === state.count - 1;
    label.textContent = !ready ? 'Connecting…' : (adapter.label?.(state.index) || `${state.index + 1} / ${state.count}`);
    label.title = ready ? `Step ${state.index + 1} of ${state.count}` : 'Presentation controls reconnect automatically';
  }
  function mount() {
    if (bar || !document.body) return;
    // Shadow DOM keeps controls at touch size even in fixed-canvas, scaled slide designs.
    bar = document.createElement('div');bar.id = 'surface-presentation-controls';
    Object.assign(bar.style,{position:'fixed',bottom:'max(12px,env(safe-area-inset-bottom))',left:'50%',transform:'translateX(-50%)',zIndex:'2147483646'});
    const root = bar.attachShadow({mode:'open'});
    root.innerHTML = `<style>:host{color-scheme:dark}nav{display:flex;align-items:center;gap:6px;padding:4px;border:1px solid #ffffff25;border-radius:30px;background:#111925f2;color:#e8edf5;font:13px system-ui;box-shadow:0 4px 20px #0004}button{width:52px;height:48px;flex-shrink:0;border:0;border-radius:24px;background:#ffffff0c;color:inherit;font:28px system-ui;touch-action:manipulation;cursor:pointer}button:disabled{opacity:.3;cursor:default}button:focus-visible{outline:2px solid #6ea8fe}span{min-width:64px;max-width:150px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis} @media(pointer:fine){nav{opacity:.35;transition:opacity .15s}nav:hover,nav:focus-within{opacity:1}}</style><nav aria-label="Presentation controls"><button type="button" aria-label="Previous presentation step">‹</button><span role="status" aria-live="polite"></span><button type="button" aria-label="Next presentation step">›</button></nav>`;
    [previous,next] = root.querySelectorAll('button');label = root.querySelector('span');
    previous.onclick = () => request('previous');next.onclick = () => request('next');
    document.body.appendChild(bar);render();
  }
  window.surface.presentation = {
    register(config) {
      if (!config || typeof config.id !== 'string' || !config.id || config.id.length > 120 || !Number.isSafeInteger(config.count) || config.count < 1 || config.count > 10000 || typeof config.render !== 'function') throw new Error('Presentation needs id, count and render(index).');
      adapter = config;state = null;registered = false;
      document.documentElement.style.touchAction='pan-y pinch-zoom';
      if (document.body) mount();else document.addEventListener('DOMContentLoaded',mount,{once:true});
      window.surface.emit('__presentation-ready');
      render();return {next:()=>request('next'),previous:()=>request('previous'),go:index=>request('go',index)};
    }
  };
  addEventListener('message', e => {
    if (e.source !== parent || !e.data?.__surfacePresentation || !adapter) return;
    const data = e.data.__surfacePresentation;
    online = data.online === true;
    if (!online) registered = false;
    if (online && !registered) {
      registered = true;window.surface.emit('__presentation-register',{deck:adapter.id,count:adapter.count});
    }
    const incoming = data.state;
    if (incoming?.deck === adapter.id && incoming.count === adapter.count && Number.isSafeInteger(incoming.index) && incoming.index >= 0 && incoming.index < adapter.count && (!state || incoming.revision >= state.revision)) {
      const changed = !state || incoming.index !== state.index;state = incoming;
      if (changed) adapter.render(state.index);
    }
    render();
  });
  addEventListener('keydown', e => {
    if (!adapter || e.altKey || e.ctrlKey || e.metaKey || editable(e.target)) return;
    const cmd = ['ArrowRight','ArrowDown','PageDown',' '].includes(e.key) ? 'next' : ['ArrowLeft','ArrowUp','PageUp'].includes(e.key) ? 'previous' : ['Home','End'].includes(e.key) ? 'go' : null;
    if (!cmd) return;e.preventDefault();e.stopImmediatePropagation();
    if (!e.repeat) request(cmd,e.key === 'End' ? adapter.count-1 : 0);
  },true);
  addEventListener('pointerdown', e => {start = adapter && e.isPrimary && e.pointerType === 'touch' && !editable(e.target) && !e.composedPath().includes(bar) ? {x:e.clientX,y:e.clientY} : null;},{passive:true});
  addEventListener('pointercancel',()=>{start=null;},{passive:true});
  let swallowClick = false;
  addEventListener('pointerup', e => {
    if (!start) return;const dx=e.clientX-start.x,dy=e.clientY-start.y;start=null;
    if (Math.abs(dx)>60 && Math.abs(dx)>Math.abs(dy)*2) {request(dx<0?'next':'previous');swallowClick=true;setTimeout(()=>{swallowClick=false;},350);}
  },{passive:true});
  addEventListener('click', e => {if(swallowClick){e.preventDefault();e.stopImmediatePropagation();swallowClick=false;}},true);
})();
