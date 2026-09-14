/* Explicit human input. This socket is opened only by the Control screen button. */
(() => {
  class HumanScreen {
    constructor(h){
      this.h=h;this.frame=h.canvas;this.button=document.createElement('button');this.button.className='small';this.button.textContent='Control screen';this.button.hidden=h.readonly();h.toolbar.appendChild(this.button);
      this.button.onclick=()=>this.ws?this.release():this.acquire();
      this.frame.tabIndex=0;this.frame.setAttribute('aria-label','Remote screen. Enable control to interact.');
      this.frame.addEventListener('click',e=>this.pointer(e,'left_click'));
      this.frame.addEventListener('contextmenu',e=>{if(this.owned){e.preventDefault();this.pointer(e,'right_click');}});
      this.frame.addEventListener('wheel',e=>{if(this.owned){e.preventDefault();this.pointer(e,'scroll',{amount:e.deltaY>0?-3:3});}},{passive:false});
      this.frame.addEventListener('pointermove',e=>{if(this.owned){this.move={action:'mouse_move',coordinate:this.coordinate(e)};this.flushMove();}});
      this.frame.addEventListener('keydown',e=>this.key(e));
      this.onBlur=()=>this.release();window.addEventListener('blur',this.onBlur);window.addEventListener('pagehide',this.onBlur);
      this.text=document.createElement('button');this.text.className='small';this.text.textContent='Type text';this.text.hidden=true;h.toolbar.appendChild(this.text);
      this.text.onclick=()=>{const value=prompt('Text to enter on this machine');if(value)this.send({action:'type',text:value});this.frame.focus();};
    }
    coordinate(e){const r=this.frame.getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))];}
    acquire(){
      if(this.h.readonly())return;
      this.button.textContent='Connecting…';const ws=new WebSocket(this.h.url());this.ws=ws;
      ws.onmessage=e=>{if(this.ws!==ws)return;const m=JSON.parse(e.data);
        if(m.type==='control_ready'){this.geometry=m.geometry;this.clockOffset=m.server_time*1000-Date.now();this.owned=true;this.button.textContent='End control · you control';this.text.hidden=false;this.frame.focus();this.timer=setInterval(()=>{if(ws.readyState===1)ws.send(JSON.stringify({type:'ping'}));},5000);}
        if(m.type==='geometry')this.geometry=m.geometry;
        if(m.type==='ack'){this.waiting=false;this.flush();}
        if(m.type==='error'){this.h.error(m.error);this.waiting=false;this.queue=[];if(m.geometry)this.geometry=m.geometry;}
      };
      ws.onclose=()=>{if(this.ws===ws)this.release();};ws.onerror=()=>this.h.error('Screen control unavailable');
    }
    send(input){
      if(!this.owned)return;this.queue||=[];
      if(this.queue.length>=16)return;
      this.queue.push({input,time:Date.now(),revision:this.frame.remoteFrame?.revision});this.flush();
    }
    flush(){
      if(this.waiting||!this.owned||this.ws?.readyState!==1)return;
      const frame=this.frame.remoteFrame;if(!frame||frame.revision!==this.geometry.revision)return;
      const item=this.queue?.shift();
      if(item&&(Date.now()-item.time>2000||item.revision!==frame.revision)){this.flush();return;}
      const input=item?.input||this.move;if(!input)return;
      if(!item)this.move=null;this.waiting=true;
      this.ws.send(JSON.stringify({...input,type:'input',screen:this.geometry.id,geometry:frame.revision,frame_at:frame.at,at:(Date.now()+this.clockOffset)/1000}));
    }
    flushMove(){this.flush();}
    pointer(e,action,extra={}){if(!this.owned)return;e.preventDefault();e.stopImmediatePropagation();this.frame.focus();this.send({action,coordinate:this.coordinate(e),...extra});}
    key(e){
      if(!this.owned)return;if(e.key==='Escape'){e.preventDefault();this.release();return;}
      if(['Shift','Control','Alt','Meta'].includes(e.key))return;
      e.preventDefault();e.stopPropagation();
      const names={ArrowLeft:'left',ArrowRight:'right',ArrowUp:'up',ArrowDown:'down',Backspace:'backspace',Delete:'delete',Enter:'enter',Tab:'tab',Home:'home',End:'end',PageUp:'pageup',PageDown:'pagedown',' ':'space'};
      if(e.key.length===1&&!e.ctrlKey&&!e.metaKey&&!e.altKey)this.send({action:'type',text:e.key});
      else this.send({action:'key',text:[e.ctrlKey?'ctrl':null,e.metaKey?'super':null,e.altKey?'alt':null,e.shiftKey?'shift':null,names[e.key]||e.key.toLowerCase()].filter(Boolean).join('+')});
    }
    release(){clearInterval(this.timer);const ws=this.ws;this.ws=null;this.owned=false;this.waiting=false;this.move=null;this.queue=[];this.text.hidden=true;this.button.textContent='Control screen';ws?.close();}
    destroy(){this.release();window.removeEventListener('blur',this.onBlur);window.removeEventListener('pagehide',this.onBlur);}
  }
  window.ConductorHumanScreen=HumanScreen;
})();
