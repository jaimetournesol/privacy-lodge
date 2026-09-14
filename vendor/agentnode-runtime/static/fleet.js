/* Orbital fleet: stable relationships, names only, motion driven by real activity. */
(() => {
  'use strict';
  const NS='http://www.w3.org/2000/svg';
  const labels={working:'Working',available:'Available',starting:'Starting',queued:'Queued',waiting:'Waiting for you',stopped:'Stopped',unreachable:'Unreachable'};
  function state(node,p){
    if(!node.reachable)return 'unreachable';
    if(p.status==='starting')return 'starting';
    if(!p.alive)return 'stopped';
    if(p.status==='waiting')return 'waiting';
    if(p.status==='working')return 'working';
    if(p.queued)return 'queued';
    return 'available';
  }
  function html(tag,cls,parent,text){const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=text;parent?.appendChild(n);return n;}
  function svg(tag,attrs,parent,text){const n=document.createElementNS(NS,tag);for(const [k,v]of Object.entries(attrs||{}))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;parent?.appendChild(n);return n;}
  function button(g,label,action){g.setAttribute('role','button');g.setAttribute('tabindex','0');g.setAttribute('aria-label',label);g.onclick=action;g.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();action();}};}
  const short=(s,n)=>s.length>n?s.slice(0,n-1)+'…':s;
  function fitText(el,name,width){
    if(el._fullName===name&&el._width===width)return;
    el._fullName=name;el._width=width;el.textContent=name;
    let n=name.length;while(n>1&&el.getComputedTextLength()>width)el.textContent=short(name,--n);
  }
  class Overview {
    constructor(root,getNodes,onSelect){
      Object.assign(this,{root,getNodes,onSelect,rows:new Map(),systems:new Map(),effects:new Set(),closed:false,visible:true,key:'',waiting:false});
      this.id='fleet-'+Math.random().toString(36).slice(2);
      root.className='fleet-overview';root.setAttribute('aria-label','Fleet constellation');
      this.scene=html('div','gx-scene',root);
      this.svg=svg('svg',{class:'gx-sky',role:'group','aria-label':'Conductor, machines and agents'},this.scene);
      this.event=html('div','gx-event',root);this.event.setAttribute('role','status');
      this.panel=html('div','gx-inspector',root);this.panel.hidden=true;
      this.motion=matchMedia('(prefers-reduced-motion: reduce)');
      this.onMotion=()=>{this.clearEffects();this.pause();};this.motion.addEventListener('change',this.onMotion);
      this.resize=new ResizeObserver(()=>this.schedule());this.resize.observe(root);
      for(const selector of ['#voHud','#voTop']){const el=document.querySelector(selector);if(el)this.resize.observe(el);}
      this.intersection=new IntersectionObserver(entries=>{this.visible=entries[0].isIntersecting;this.pause();if(this.visible)this.tick();});this.intersection.observe(root);
      this.onVisibility=()=>{this.pause();if(!document.hidden)this.tick();};document.addEventListener('visibilitychange',this.onVisibility);
      this.timer=setInterval(()=>this.tick(),1000);this.tick();
    }
    pause(){const paused=document.hidden||!this.visible;this.root.dataset.paused=String(paused);if(paused)this.clearEffects();}
    schedule(){if(!this.frame&&!this.closed)this.frame=requestAnimationFrame(()=>{this.frame=0;this.tick();});}
    clearEffects(){for(const e of this.effects){e.animation.cancel();e.el.remove();}this.effects.clear();}
    closePanel(restore=true){this.panel.hidden=true;if(restore)this.panelTrigger?.focus();this.panelTrigger=null;}
    inspect(nodeName,trigger){
      const node=this.getNodes().find(n=>n.name===nodeName);if(!node)return;
      this.panel.replaceChildren();this.panel.hidden=false;this.panelTrigger=trigger;
      this.panel.setAttribute('role','dialog');this.panel.setAttribute('aria-label',node.name);
      const top=html('div','gx-inspector-head',this.panel);html('strong','',top,node.name);
      const close=html('button','',top,'×');close.setAttribute('aria-label','Close machine details');close.onclick=()=>this.closePanel();
      html('p','gx-inspector-status',this.panel,node.reachable?'Connected':'Unreachable');
      for(const p of node.projects||[]){const b=html('button','gx-inspector-agent',this.panel);html('span','',b,p.name||p.id);html('small','',b,labels[state(node,p)]);b.onclick=()=>{this.closePanel(false);this.onSelect(node.name,p.id);};}
      this.panel.onkeydown=e=>{if(e.key==='Escape'){e.stopPropagation();this.closePanel();}};close.focus();
    }
    build(nodes,mainNode,main){
      const voice=document.body.classList.contains('voicemode');
      const rootBox=this.root.getBoundingClientRect();
      // Reserve the actual overlay intersection, including when Fleet is one of several stage tiles.
      const hud=voice?document.querySelector('#voHud')?.getBoundingClientRect():null;
      const topBar=voice?document.querySelector('#voTop')?.getBoundingClientRect():null;
      const sideBySide=voice&&document.body.classList.contains('mobile')&&matchMedia('(orientation:landscape)').matches;
      const overlaps=hud&&hud.right>rootBox.left&&hud.left<rootBox.right&&hud.bottom>rootBox.top&&hud.top<rootBox.bottom;
      const right=overlaps&&sideBySide?Math.max(0,rootBox.right-Math.max(rootBox.left,hud.left)):0;
      const bottom=overlaps&&!sideBySide?Math.max(0,rootBox.bottom-Math.max(rootBox.top,hud.top)):0;
      // Voice now gives Stage its own viewport. A toolbar below or beside it
      // must not reserve the entire Fleet height as if it were an overlay.
      const topOverlaps=topBar&&topBar.right>rootBox.left&&topBar.left<rootBox.right&&topBar.bottom>rootBox.top&&topBar.top<rootBox.bottom;
      const top=topOverlaps?Math.max(0,Math.min(rootBox.height,topBar.bottom-rootBox.top+10)):0;
      this.scene.style.inset=`${top}px ${right}px ${bottom}px 0`;
      this.panel.style.top=(top+12)+'px';this.panel.style.right=(right+12)+'px';
      this.panel.style.width=`min(290px,calc(100% - ${right+24}px))`;this.panel.style.maxHeight=`calc(100% - ${top+bottom+24}px)`;
      const width=Math.max(1,this.root.clientWidth-right),available=Math.max(1,this.root.clientHeight-top-bottom);
      // One coordinate system on every device. Portrait scales this same map; it never rearranges it.
      const W=960;
      const localProjects=(mainNode?.projects||[]).filter(p=>p!==main);
      const machines=nodes.filter(n=>n.name!==mainNode?.name).map(n=>({...n,projects:n.projects||[]}));
      const groups=[[],[]];machines.forEach((n,i)=>groups[i%2].push(n));
      const block=n=>Math.max(154,n.projects.length*62+28);
      const totals=groups.map(g=>g.reduce((s,n)=>s+block(n),0));
      const H=Math.max(640,Math.max(...totals)+64,localProjects.length?(localProjects.length*62+125)*2:0);
      const scale=Math.min(width/W,available/H),fontScale=Math.min(2,Math.max(1,.85/scale));
      this.root.style.setProperty('--gx-font-scale',fontScale);
      const key=JSON.stringify([W,H,fontScale,machines.map(n=>[n.name,n.projects.map(p=>[p.id,p.name])]),mainNode?.name,main?.id,main?.name,localProjects.map(p=>[p.id,p.name])]);
      if(key===this.key)return;
      this.key=key;this.clearEffects();this.closePanel(false);this.rows.clear();this.systems.clear();this.svg.replaceChildren();
      this.svg.setAttribute('viewBox',`0 0 ${W} ${H}`);this.svg.setAttribute('preserveAspectRatio','xMidYMid meet');
      const cx=W/2,cy=H/2;this.center=[cx,cy];
      const defs=svg('defs',{},this.svg);
      const gradient=svg('radialGradient',{id:this.id+'-core'},defs);
      for(const [offset,color,opacity]of [[0,'#ffe6b6',.36],[.3,'#dbab77',.1],[1,'#dbab77',0]])svg('stop',{offset,'stop-color':color,'stop-opacity':opacity},gradient);
      const bg=svg('g',{'aria-hidden':'true',class:'gx-background'},this.svg);
      // Static, sparse stars: no moving full-screen layers, SVG blur or filters.
      let seed=1827;const rand=()=>{seed=(seed*1664525+1013904223)>>>0;return seed/4294967296;};
      for(let i=0;i<96;i++)svg('circle',{cx:rand()*W,cy:rand()*H,r:i%9===0?1.2:.6,fill:'#a1b7d6',opacity:.12+rand()*.22},bg);
      svg('ellipse',{class:'gx-orbit-guide',cx,cy,rx:W*.33,ry:H*.34},bg);
      svg('ellipse',{class:'gx-orbit-guide gx-outer-guide',cx,cy,rx:W*.42,ry:H*.44},bg);
      this.paths=svg('g',{'aria-hidden':'true',class:'gx-routes'},this.svg);
      const world=svg('g',{class:'gx-constellation'},this.svg);
      const hub=svg('g',{class:'gx-hub',transform:`translate(${cx} ${cy})`},world);this.hub=hub;
      svg('circle',{r:72,fill:`url(#${this.id}-core)`},hub);
      svg('circle',{class:'gx-hub-track',r:34},hub);svg('circle',{class:'gx-hub-track gx-hub-track-outer',r:43},hub);
      svg('circle',{class:'gx-hit',r:48},hub);svg('circle',{class:'gx-core',r:14},hub);
      svg('circle',{class:'gx-hub-highlight',r:7,cx:-3,cy:-3},hub);
      svg('circle',{class:'gx-progress',r:34,pathLength:100},hub);svg('circle',{class:'gx-beacon',r:33},hub);
      this.hubName=svg('text',{class:'gx-hub-label',x:0,y:63,'text-anchor':'middle'},hub);fitText(this.hubName,main?.name||'Conductor',Math.min(W-40,210));
      if(main)button(hub,'Open Conductor',()=>this.onSelect(mainNode.name,main.id));
      const makeAgent=(node,p,x,y,ax,ay,parent,d)=>{
        const tetherD=`M ${x} ${y} Q ${x+(ax-x)*.7} ${y} ${ax} ${ay}`;
        const tether=svg('path',{class:'gx-tether',d:tetherD},parent);
        const flow=svg('path',{class:'gx-flow',d:tetherD,pathLength:100,'aria-hidden':'true'},parent);
        const g=svg('g',{class:'fo-agent gx-agent',transform:`translate(${ax} ${ay})`},parent);
        svg('rect',{class:'gx-hit',x:-Math.min(62,(W-42)/6),y:-23,width:Math.min(124,(W-42)/3),height:58,rx:12},g);
        svg('circle',{class:'gx-star-halo',r:20},g);svg('circle',{class:'gx-star-track',r:10},g);
        svg('circle',{class:'gx-progress',r:10,pathLength:100},g);svg('circle',{class:'gx-star',r:4.5},g);svg('circle',{class:'gx-beacon',r:12},g);
        const title=svg('title',{},g),label=svg('text',{class:'gx-agent-label',x:0,y:29,'text-anchor':'middle'},g);
        button(g,p.name||p.id,()=>this.onSelect(node.name,p.id));
        this.rows.set(JSON.stringify([node.name,p.id]),{g,label,title,tether,flow,d:d+tetherD.replace(/^M [\d.\-]+ [\d.\-]+/,''),maxWidth:140});
      };
      groups.forEach((group,side)=>{
        let cursor=(H-totals[side])/2;
        group.forEach(node=>{
          const size=block(node);
          const x=W*(side===0?.30:.70),y=cursor+size/2;
          const cluster=svg('g',{class:'gx-system','data-node':node.name},world);
          const d=`M ${cx} ${cy} C ${cx+(x-cx)*.55} ${cy} ${cx+(x-cx)*.45} ${y} ${x} ${y}`;
          const route=svg('path',{class:'gx-route',d},this.paths);
          const flow=svg('path',{class:'gx-flow',d,pathLength:100,'aria-hidden':'true'},this.paths);
          const machine=svg('g',{class:'gx-machine',transform:`translate(${x} ${y})`},cluster);
          svg('circle',{class:'gx-planet-halo',r:28},machine);svg('circle',{class:'gx-planet-ring',r:19},machine);
          svg('path',{class:'gx-planet-shell',d:'M 0 -12 L 10.4 -6 L 10.4 6 L 0 12 L -10.4 6 L -10.4 -6 Z'},machine);
          svg('circle',{class:'gx-planet',r:4},machine);svg('circle',{class:'gx-hit',r:25},machine);
          const machineName=svg('text',{class:'gx-system-label',x:0,y:31,'text-anchor':'middle'},machine);fitText(machineName,node.name,156);
          button(machine,node.name,()=>this.inspect(node.name,machine));
          this.systems.set(node.name,{group:cluster,machine,route,flow,d});
          node.projects.forEach((p,j)=>{
            const ax=x+(side===0?-1:1)*(192+8*Math.cos(j)),ay=y+(j-(node.projects.length-1)/2)*62;
            makeAgent(node,p,x,y,ax,ay,cluster,d);
          });
          cursor+=size;
        });
      });
      // The hub also represents its host machine. Other local agents remain visible without a duplicate Mac node.
      localProjects.forEach((p,j)=>makeAgent(mainNode,p,cx,cy,cx,cy+105+j*62,world,`M ${cx} ${cy}`));
      this.effectsLayer=svg('g',{'aria-hidden':'true',class:'gx-effects'},this.svg);
    }
    tick(){
      if(this.closed||document.hidden||!this.visible)return;
      const nodes=this.getNodes()||[],mainNode=nodes.find(n=>(n.projects||[]).some(p=>p.conductor)),main=mainNode?.projects.find(p=>p.conductor);
      this.build(nodes,mainNode,main);
      for(const node of nodes){
        const system=this.systems.get(node.name);
        if(system){
          system.group.dataset.reachable=String(!!node.reachable);system.route.dataset.reachable=String(!!node.reachable);
          const working=(node.projects||[]).some(p=>state(node,p)==='working');
          system.route.dataset.state=system.flow.dataset.state=working?'working':'available';
          system.machine.setAttribute('aria-label',`${node.name}: ${node.reachable?'connected':'unreachable'}. Open machine details`);
        }
        for(const p of node.projects||[]){const row=this.rows.get(JSON.stringify([node.name,p.id]));if(!row)continue;
          const s=state(node,p),name=p.name||p.id;row.g.dataset.state=s;row.tether.dataset.state=s;row.flow.dataset.state=s;
          fitText(row.label,name,row.maxWidth);
          const detail=`${name} on ${node.name}: ${labels[s]}${s==='working'&&p.tool?' · '+p.tool:''}`;
          if(row.title.textContent!==detail)row.title.textContent=detail;
          row.g.setAttribute('aria-label',`${detail}. Open conversation`);
        }
      }
      const hs=main?state(mainNode,main):'unreachable';this.hub.dataset.state=hs;this.hub.dataset.waiting=String(this.waiting&&hs!=='unreachable'&&hs!=='stopped');
      this.hub.setAttribute('aria-label',`${main?.name||'Conductor'}: ${this.waiting?'Waiting for you':labels[hs]}. Open conversation`);
    }
    pulse(d,kind){
      if(!d||this.closed||document.hidden||!this.visible||this.motion.matches)return;
      // Finite effects, capped during bursts. No synthetic work or perpetual route traffic.
      if(this.effects.size>=16){const old=this.effects.values().next().value;old.animation.cancel();old.el.remove();this.effects.delete(old);}
      const el=svg('path',{class:'gx-packet','data-kind':kind,d,pathLength:100},this.effectsLayer);
      const inbound=kind==='done'||kind==='error';
      const animation=el.animate([{strokeDashoffset:inbound?-100:5,opacity:0},{opacity:1,offset:.12},{opacity:1,offset:.85},{strokeDashoffset:inbound?5:-100,opacity:0}],{duration:1600,easing:'ease-in-out'});
      const effect={el,animation};this.effects.add(effect);animation.onfinish=()=>{el.remove();this.effects.delete(effect);};
    }
    flash(target){
      if(!target||this.closed||document.hidden||!this.visible||this.motion.matches)return;
      target.classList.remove('gx-changed');void target.getBoundingClientRect();target.classList.add('gx-changed');
    }
    notify(node,kind,text,project){
      if(this.closed)return;
      if(kind==='ask')this.waiting=true;else if(!node&&['send','say'].includes(kind))this.waiting=false;
      this.tick();
      this.event.textContent=[node,text||({send:'Task sent',done:'Task completed',error:'Task needs attention',focus:'Showing activity',say:'Speaking',ask:'Waiting for your answer'}[kind]||'Activity updated')].filter(Boolean).join(' · ');
      const system=this.systems.get(node),row=project?this.rows.get(JSON.stringify([node,project])):null;
      if(['send','done','error'].includes(kind))this.pulse(row?.d||system?.d,kind);
      this.flash(row?.g||system?.machine||this.hub);
    }
    destroy(){this.closed=true;this.clearEffects();clearInterval(this.timer);cancelAnimationFrame(this.frame);this.resize.disconnect();this.intersection.disconnect();this.motion.removeEventListener('change',this.onMotion);document.removeEventListener('visibilitychange',this.onVisibility);}
  }
  window.ConductorFleet=Object.freeze({Overview,state});
})();
