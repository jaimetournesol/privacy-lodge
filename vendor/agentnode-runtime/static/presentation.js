/* Explicit presentation coordination; ordinary browsing remains local. */
(() => {
  'use strict';
  class Presentation {
    constructor(hooks){
      this.h=hooks;this.active=false;this.ready=false;this.online=false;this.applying=false;this.latest=null;this.pending=null;this.generation=0;
      this.source=crypto.randomUUID();this.display=null;this.presenterId=null;this.scopeEpoch=0;
      const requested=new URL(location.href).searchParams.get('screen');
      if(this.h.readOnly?.())this.display=requested;
      this.outputId=/^[a-zA-Z0-9_-]{1,64}$/.test(requested||'')?requested:this.source;
      try{if(performance.getEntriesByType('navigation')[0]?.type==='reload')this.previousConnection=sessionStorage.getItem('presenter-connection:'+this.outputId);}catch{}
      this.screenSelect=document.querySelector('#presentationScreen');
      this.displayName=document.querySelector('#presentationName');this.displaySelect=document.querySelector('#displayTarget');
      if(this.h.displays){
        try{this.displayName.value=sessionStorage.getItem('presenter-name:'+this.outputId)||'';}catch{this.displayName.value='';}
        if(this.screenSelect)this.screenSelect.onchange=()=>this.reclaim(this.screenSelect.value);
        this.displayName.onchange=()=>this.heartbeat();
        this.displaySelect.onchange=()=>this.selectDisplay(this.displaySelect.value||null);
        this.displayTimer=setInterval(()=>{this.heartbeat();this.loadDisplays();},20000);
        window.addEventListener('pagehide',()=>this.leaveDisplay());
        this.loadDisplays();
      }
      this.reconcileTimer=setInterval(()=>{if(this.ready&&(this.active||this.h.canFollow?.())&&!document.hidden){this.refresh();this.follow();}},5000);this.toolbar=document.querySelector('#presentationBar');this.status=document.querySelector('#presentationStatus');
      document.querySelector('#btnPresent').onclick=()=>this.enter(true);
      document.querySelector('#presentationFullscreen').onclick=()=>this.fullscreen();
      document.querySelector('#presentationExit').onclick=()=>this.exit();
      document.addEventListener('pointermove',()=>this.reveal(),{passive:true});
      document.addEventListener('keydown',e=>{if(!this.active)return;this.reveal();if(e.key==='Escape'&&!document.fullscreenElement)this.exit();});
      document.addEventListener('fullscreenchange',()=>{document.querySelector('#presentationFullscreen').textContent=document.fullscreenElement?'Windowed':'Fullscreen';this.reveal();});
    }
    async loadDisplays(){
      if(!this.h.displays||this.h.readOnly?.())return;
      try{
        const result=await this.h.displays();
        this.displaySelect.replaceChildren(new Option('Follow conductor',''));
        for(const item of result.items||[])this.displaySelect.add(new Option(item.label||item.name,item.id));
        if(this.display&&!this.active&&!Array.from(this.displaySelect.options).some(o=>o.value===this.display)){await this.selectDisplay(null);}
        this.displaySelect.value=this.display||'';
        if(this.screenSelect){
          this.screenSelect.replaceChildren(new Option('New screen',''));
          for(const item of result.known||result.items||[])this.screenSelect.add(new Option((item.label||item.name),item.id));
          this.screenSelect.value=this.presenterId||'';
        }
        this.h.outputs?.(result);
      }catch{/* Preserve the selected scope during a network outage. */}
    }
    async selectDisplay(id){
      if(this.active||this.publishing){this.displaySelect.value=this.display||'';return;}
      this.display=id;this.scopeEpoch++;this.latest=null;this.lastSent=null;this.generation++;
      clearTimeout(this.pending);this.pending=null;await this.refresh();
    }
    async reclaim(id){
      if(!this.active||this.registering)return;
      this.leaveDisplay();this.outputId=id||crypto.randomUUID();this.displayName.value='';
      await this.heartbeat();
    }
    async heartbeat(){
      if(this.h.readOnly?.()||!this.active||!this.h.registerDisplay||this.h.isPhone?.()||this.registering)return;
      this.registering=true;
      try{
        const output=this.outputId;
        const item=await this.h.registerDisplay(output,this.displayName.value.trim(),this.source,this.previousConnection);
        if(!this.active){await this.h.leaveDisplay(output,this.source);return;}
        this.displayName.value=item.name;
        this.previousConnection=null;
        try{sessionStorage.setItem('presenter-name:'+output,item.name);sessionStorage.setItem('presenter-connection:'+output,this.source);}catch{}
        if(item.id&&!this.presenterId){
          this.presenterId=item.id;this.display=item.id;this.scopeEpoch++;this.latest=null;this.lastSent=null;this.generation++;
          const url=new URL(location.href);url.searchParams.set('screen',item.id);history.replaceState(null,'',url);
          await this.refresh();
        }
        await this.loadDisplays();await this.acknowledge();
      }catch(error){this.status.textContent=error.message||'Display registration unavailable';this.reveal();}
      finally{this.registering=false;}
    }
    leaveDisplay(){
      if(this.presenterId)this.h.leaveDisplay?.(this.presenterId,this.source).catch(()=>{});
      this.presenterId=null;
    }
    async acknowledge(){
      if(!this.h.readOnly?.()&&this.active&&this.presenterId&&this.latest&&this.h.ackDisplay){
        try{await this.h.ackDisplay(this.presenterId,this.source,this.latest.revision);}catch{/* Next heartbeat retries. */}
      }
    }
    signature(){return JSON.stringify(this.h.serialize());}
    reveal(){if(!this.active)return;document.body.classList.remove('projection-quiet');clearTimeout(this.hideTimer);if(this.online)this.hideTimer=setTimeout(()=>document.body.classList.add('projection-quiet'),3000);}
    connected(on){this.online=on;this.status.textContent=on?'Live stage':'Reconnecting…';this.toolbar.classList.toggle('offline',!on);this.reveal();if(!on){clearTimeout(this.pending);this.pending=null;this.generation++;}}
    accept(snapshot){
      if(!snapshot||!Number.isSafeInteger(snapshot.revision)||!Array.isArray(snapshot.tiles)||snapshot.tiles.length>6)return false;
      if(this.latest&&snapshot.revision<this.latest.revision)return false;
      const reset=snapshot.last_reset_revision||0;
      const previous=this.latest?.last_reset_revision||0;
      this.latest=snapshot;if(reset>previous)this.h.onReset?.();this.h.snapshot?.(snapshot);return true;
    }
    control(event,next){
      if(event.type==='displays_changed'){this.loadDisplays();return;}
      // Each scoped view fetches its effective composition. Untargeted events
      // may change the default while this output retains its assigned composition.
      if(event.presentation&&this.display){
        this.refresh();
        return;
      }
      const snapshot=event.presentation;
      if(snapshot){
        if(!this.accept(snapshot))return;
        if(event.source!==this.source){clearTimeout(this.pending);this.pending=null;this.generation++;this.lastSent=this.signature();}
        if(event.source!==this.source&&(this.active||this.h.canFollow?.())){this.apply(snapshot);this.lastSent=this.signature();this.acknowledge();}
      }
      if(event.type==='presentation_stage'||((this.active||this.h.canFollow?.())&&snapshot))return;
      if(['focus','stage','pin'].includes(event.type)&&!this.active&&!this.h.canFollow?.())return;
      this.applying=true;
      try{next();}finally{this.applying=false;if(['focus','stage','pin'].includes(event.type))this.lastSent=this.signature();}
    }
    apply(snapshot){
      this.applying=true;
      try{if(snapshot.revision===0&&!snapshot.tiles.length)this.h.fallback();else this.h.apply(snapshot.tiles);}finally{this.applying=false;}
    }
    async follow(epoch=this.followEpoch){
      if(!(this.active||this.h.canFollow?.())||epoch!==this.followEpoch||this.following)return;
      this.following=true;
      try{if(this.online&&!document.hidden)await this.h.follow?.();}catch{/* Retry the visible workspaces on the next pass. */}
      finally{this.following=false;}
    }
    async refresh(){
      if(this.refreshTask)return this.refreshTask;
      this.refreshTask=this._refresh();
      try{return await this.refreshTask;}finally{this.refreshTask=null;}
    }
    async _refresh(){
      if(this.refreshing)return;
      this.refreshing=true;const scope=this.scopeEpoch;
      try{
        const state=await this.h.get(this.display);
        if(scope!==this.scopeEpoch)return;
        // An in-flight phone edit resolves through its receipt or conflict response.
        if(this.pending||this.publishing)return;
        if(this.accept(state)&&(this.active||this.h.canFollow?.())){
          const mismatch=JSON.stringify(state.tiles)!==this.signature();
          if(mismatch){this.apply(state);this.lastSent=this.signature();}
          this.acknowledge();
        }
        this.status.textContent=this.online?'Live stage':'Reconnecting…';this.toolbar.classList.toggle('offline',!this.online);
      }
      catch{this.status.textContent='Stage sync unavailable — reconnecting';this.toolbar.classList.add('offline');document.body.classList.remove('projection-quiet');}
      finally{this.refreshing=false;if(scope!==this.scopeEpoch)setTimeout(()=>this.refresh(),0);}
    }
    changed(){
      if(!this.ready||this.applying||!this.online||!this.latest||!this.h.canPublish())return;
      clearTimeout(this.pending);const generation=this.generation;
      this.pending=setTimeout(()=>{this.pending=null;this.publish(generation);},120);
    }
    async publishView(){
      if(this.publishing||this.active||this.h.readOnly?.())return;
      await this.refresh();if(!this.latest)throw new Error('Presentation unavailable');
      const tiles=this.h.serialize(),revision=this.latest.revision;this.publishing=true;
      try{
        let response=await this.h.post({tiles,base_revision:revision,source:this.source,...(this.display?{display:this.display}:{})});
        let data=await response.json();
        if(data.code==='needs_display_selection'){
          const display=await this.h.chooseDisplay?.(data.displays);if(!display)return;
          response=await this.h.post({tiles,base_revision:revision,source:this.source,display});data=await response.json();
        }
        if(!response.ok)throw new Error(data.error||'Presentation changed. Review the destination and publish again.');
        this.accept(data);this.h.message?.('Shown in presentation');
      }finally{this.publishing=false;}
    }
    async publish(generation){
      if(generation!==this.generation||!this.online||!this.h.canPublish()||this.publishing)return;
      const tiles=this.h.serialize(),signature=JSON.stringify(tiles);
      if(signature===this.lastSent)return;
      const revision=this.latest.revision;this.lastSent=signature;this.publishing=true;
      try{
        const response=await this.h.post({tiles,base_revision:revision,source:this.source,...(this.display?{display:this.display}:{})});
        const data=await response.json();
        if(response.ok){this.accept(data);if(this.signature()===signature){if(this.h.canFollow?.())this.apply(data);this.lastSent=this.signature();}else this.lastSent=signature;}
        else if(data.code==='needs_display_selection'){
          this.lastSent=null;if(data.presentation){this.accept(data.presentation);this.apply(data.presentation);}
          this.chooseDestination(tiles,revision,data.displays);
        }
        else {this.lastSent=null;if(data.presentation){if(this.accept(data.presentation)&&this.h.canFollow?.()){this.apply(data.presentation);this.lastSent=this.signature();}this.generation++;}}
      }catch{this.lastSent=null;}
      finally{this.publishing=false;if(generation===this.generation&&this.lastSent&&this.signature()!==signature)this.changed();}
    }
    async chooseDestination(tiles,revision,displays){
      const display=await this.h.chooseDisplay?.(displays);
      if(!display)return;
      try{
        const response=await this.h.post({tiles,base_revision:revision,source:this.source,display});
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'Stage changed. Please try again.');
        this.display=null;this.scopeEpoch++;this.accept(data);this.apply(data);this.lastSent=this.signature();await this.loadDisplays();
      }catch(error){this.h.message?.(error.message);this.refresh();}
    }
    async reset(){
      if(!this.online||!this.latest)throw new Error('Stage is offline. Reconnect and try again.');
      if(this.publishing)throw new Error('Stage is syncing. Try Reset Stage again in a moment.');
      clearTimeout(this.pending);this.pending=null;this.generation++;this.publishing=true;
      try{
        const response=await this.h.post({action:'reset',base_revision:this.latest.revision,source:this.source,...(this.display?{display:this.display}:{})});
        const data=await response.json();
        if(!response.ok){
          if(data.presentation&&this.accept(data.presentation)&&(this.active||this.h.canFollow?.())){this.apply(data.presentation);this.lastSent=this.signature();}
          throw new Error(response.status===409?'Stage changed. Review it and tap Reset Stage again.':'Could not reset Stage. Please try again.');
        }
        if(!this.accept(data))throw new Error('Stage changed while resetting. Review its latest contents.');
        if(this.active||this.h.canFollow?.())this.apply(data);this.lastSent=this.signature();return data;
      }finally{this.publishing=false;}
    }
    async enter(fullscreen=false){
      if(this.active||(this.h.isPhone?.()&&!this.h.readOnly?.()))return;
      if(this.publishing)return;
      this.savedPrivate=this.h.savePrivate?.();
      this.savedDisplay=this.display;
      this.followEpoch=(this.followEpoch||0)+1;this.savedChat=document.body.classList.contains('showchat');this.active=true;
      this.h.quiet();document.body.classList.add('projection');document.body.classList.remove('showchat');
      const url=new URL(location.href);url.searchParams.set('present','1');history.replaceState(null,'',url);
      if(!this.pending&&!this.publishing&&this.latest)this.apply(this.latest);
      this.reveal();if(fullscreen)this.fullscreen();const epoch=this.followEpoch;
      if(this.pending){clearTimeout(this.pending);this.pending=null;await this.publish(this.generation);}
      if(!this.active||epoch!==this.followEpoch)return;
      await this.heartbeat();await this.refresh();this.follow(epoch);
    }
    async fullscreen(){
      try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen();}
      catch{this.status.textContent='Presentation ready — use your browser’s fullscreen command';document.body.classList.remove('projection-quiet');}
    }
    exit(){
      if(!this.active||this.h.readOnly?.())return;
      this.active=false;this.leaveDisplay();this.display=this.savedDisplay||null;this.scopeEpoch++;this.latest=null;this.lastSent=null;this.generation++;clearTimeout(this.pending);this.pending=null;this.refresh();this.loadDisplays();clearTimeout(this.hideTimer);clearTimeout(this.followTimer);document.body.classList.remove('projection','projection-quiet');document.body.classList.toggle('showchat',this.savedChat);
      const url=new URL(location.href);url.searchParams.delete('present');history.replaceState(null,'',url);
      if(document.fullscreenElement)document.exitFullscreen().catch(()=>{});
      this.applying=true;try{if(this.savedPrivate)this.h.restorePrivate?.(this.savedPrivate);this.h.restore();}finally{this.applying=false;}
      document.querySelector('#btnPresent').focus();
    }
  }
  window.ConductorPresentation=Presentation;
})();
