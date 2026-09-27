/* Per-agent transcript cache and paged reads; never issues runtime commands. */
(() => {
  class HistoryView {
    constructor(h) { this.h=h; this.cache=new Map(); this.generation=0; this.version=0; }
    select(key,url) {
      this.generation++;this.key=key;this.url=url;
      this.state=this.cache.get(key)||{events:[],cursor:null,older:null,loaded:false};
      this.cache.delete(key);this.cache.set(key,this.state);
      while(this.cache.size>6)this.cache.delete(this.cache.keys().next().value);
      this.render();return this.state.cursor;
    }
    clear(){this.cache.clear();this.state=null;this.generation++;}
    receive(ev) {
      if(!this.state)return;
      this.version++;
      if(ev.type==='history_page') {
        this.state.loaded=true;
        if(!ev.append)this.state.events=[];
        this.merge(ev.events||[]);
        if(!ev.append)this.state.older=ev.older;
        this.state.cursor=ev.cursor;if(!ev.append||(ev.events||[]).length)this.render();return true;
      }
      if(ev.event_id){
        if(this.state.events.some(e=>e.event_id===ev.event_id))return true;
        this.merge([ev]);this.state.cursor=ev.cursor||this.state.cursor;
      }
      return false;
    }
    async refresh() {
      const generation=this.generation,version=this.version,state=this.state;
      if(!state)return;
      let after=state.cursor;
      try {
        let page;
        try { page=await this.h.get(this.url+(after?'?after='+encodeURIComponent(after):'')); }
        catch(error) {
          if(!after||generation!==this.generation||version!==this.version)throw error;
          // A cleared/rotated transcript invalidates its previous cursor.
          after=null;page=await this.h.get(this.url);
        }
        // A late HTTP read must never replace another agent or newer socket events.
        if(generation!==this.generation||version!==this.version)return;
        if(!Array.isArray(page.events))return;
        this.receive({type:'history_page',append:!!after,...page});
      } catch(e) {
        if(generation!==this.generation||version!==this.version)return;
        const label=this.h.element.querySelector('.history-loading');
        if(label)label.textContent='Conversation unavailable. Retrying…';
      }
    }
    merge(events) {
      const known=new Set(this.state.events.map(e=>e.event_id).filter(Boolean));
      for(const e of events)if(!e.event_id||!known.has(e.event_id)){this.state.events.push(e);known.add(e.event_id);}
    }
    render(anchor=false) {
      const el=this.h.element,oldHeight=el.scrollHeight,oldTop=el.scrollTop;
      if(anchor)this.h.beforeOlder?.();
      this.h.render({type:'history',events:this.state.events});
      if(!this.state.loaded&&!this.state.events.length){const label=document.createElement('div');label.className='history-loading';label.setAttribute('role','status');label.textContent='Loading conversation…';el.append(label);}
      if(this.state.older){const b=document.createElement('button');b.className='small history-older';b.textContent='Load older messages';b.onclick=()=>this.older(b);el.prepend(b);}
      if(anchor)el.scrollTop=oldTop+el.scrollHeight-oldHeight;
    }
    async older(button) {
      const generation=this.generation;button.disabled=true;
      try {
        const page=await this.h.get(this.url+'?before='+encodeURIComponent(this.state.older));
        if(generation!==this.generation)return;
        const existing=this.state.events;this.state.events=[];this.merge(page.events);this.merge(existing);this.state.older=page.older;this.render(true);
      } catch(e){if(generation===this.generation)this.h.error(e.message);}
      finally{button.disabled=false;}
    }
  }
  window.ConductorHistoryView=HistoryView;
})();
