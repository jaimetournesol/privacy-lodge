/* A fixed-agent view. Every operation is independently scoped by the gateway. */
(() => {
  const $=s=>document.querySelector(s);
  let me=null, socket=null, reconnect=null, statusTimer=null, transcript=null, libraries=null, connected=false, pending=null, activeWorkspace=null, live=null, sending=false;
  const notice=text=>{$('#notice').textContent=text||'';};
  async function api(path, options={}) {
    const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(me?{'X-CSRF-Token':me.csrf}:{}),...options.headers},body:options.body?JSON.stringify(options.body):undefined});
    const result=await response.json();
    if(response.status===401&&path!=='/api/login') signedOut();
    if(!response.ok)throw Error(result.error||'Request failed');
    return result;
  }
  function script(src){return new Promise((resolve,reject)=>{const s=document.createElement('script');s.src=src;s.onload=resolve;s.onerror=reject;document.head.append(s);});}
  function dependencies(){return libraries||(libraries=Promise.all(['/static/vendor/marked.js','/static/vendor/purify.min.js','/static/history-view.js'].map(script)).catch(e=>{libraries=null;throw e;}));}
  function signedOut(){
    me=null;connected=false;clearTimeout(reconnect);clearInterval(statusTimer);if(socket){socket.onclose=null;socket.close();socket=null;}
    transcript?.clear();transcript=null;pending=null;activeWorkspace=null;$('#surface').removeAttribute('src');$('#surface').hidden=true;$('#surfaceEmpty').hidden=false;
    $('#workspace').hidden=true;$('#logout').hidden=true;$('#login').hidden=false;$('#status').textContent='';$('#message').value='';$('#messages').replaceChildren();$('#detail').close();$('#detailText').textContent='';
  }
  function markdown(text){return DOMPurify.sanitize(marked.parse(String(text||'')),{FORBID_TAGS:['img','iframe','form','input','button','style'],FORBID_ATTR:['style']});}
  function render(event){
    if(event.type==='history'){ live=null;$('#messages').replaceChildren();for(const ev of event.events||[])render(ev);return; }
    const kind=event.type;let text=event.text??event.result??'', cls='', html=false;
    if(kind==='delta'&&event.delta?.type==='text_delta'){
      if(!live){live={text:'',el:document.createElement('div')};live.el.className='msg';$('#messages').append(live.el);}
      live.text+=event.delta.text;live.el.innerHTML=markdown(live.text);return;
    }
    if(kind==='user_input'){live=null;cls='user';}
    else if(kind==='assistant'){
      const el=live?.el||document.createElement('div');el.className='msg';el.replaceChildren();live=null;
      for(const block of event.message?.content||[]){
        const item=document.createElement(block.type==='tool_use'?'details':'div');
        if(block.type==='text')item.innerHTML=markdown(block.text);
        else if(block.type==='tool_use'){item.className='tool';const summary=document.createElement('summary');summary.textContent=block.name;const pre=document.createElement('pre');pre.textContent=JSON.stringify(block.input,null,2);item.append(summary,pre);}
        else continue;
        el.append(item);
      }
      $('#messages').append(el);detailButton(el,event);return;
    }
    else if(kind==='user'){
      for(const block of event.message?.content||[])if(block.type==='tool_result')render({type:'tool_result',content:block.content,detail_cursor:event.detail_cursor});return;
    }
    else if(kind==='result'){if(!event.is_error)return;cls='tool';}
    else if(kind==='tool_use'){text=event.name||event.tool||'Tool';cls='tool';}
    else if(kind==='tool_result'){cls='tool';text=typeof(event.content??event.result)==='string'?(event.content??event.result):JSON.stringify(event.content??event.result??'');}
    else if(kind==='system'||kind==='bridge'){text=event.text||event.subtype||'';cls='system';}
    else if(kind==='raw'&&event.detail_cursor){text='Large event';cls='tool';}
    else return;
    if(!text)return;
    const el=document.createElement('div');el.className='msg '+cls;
    if(html)el.innerHTML=markdown(text);else el.textContent=typeof text==='string'?text:JSON.stringify(text);
    detailButton(el,event);
    $('#messages').append(el);
  }
  function detailButton(el,event){
    if(event.detail_cursor){const b=document.createElement('button');b.className='detail';b.textContent='Load full detail';b.onclick=async()=>{try{const data=await api('/api/history?event='+encodeURIComponent(event.detail_cursor));$('#detailText').textContent=JSON.stringify(data,null,2);$('#detail').showModal();}catch(e){notice(e.message);}};el.append(b);}
  }
  function stream(){
    if(!me)return;
    const after=transcript.state?.cursor;socket=new WebSocket('wss://'+location.host+'/ws/chat'+(after?'?after='+encodeURIComponent(after):''));const current=socket;
    current.onopen=()=>{connected=true;$('#status').textContent='Connected';};
    current.onmessage=message=>{
      if(socket!==current)return;
      const ev=JSON.parse(message.data),el=$('#messages');const follow=el.scrollHeight-el.scrollTop-el.clientHeight<100;
      if(ev.type==='status'){paintStatus(ev);return;}
      if(!transcript.receive(ev))render(ev);
      if(follow)el.scrollTop=el.scrollHeight;
    };
    current.onclose=()=>{if(socket!==current||!me)return;connected=false;$('#status').textContent='Reconnecting…';reconnect=setTimeout(stream,2500);};
  }
  function paintStatus(status){
    $('#status').textContent=status.status==='working'?'Working':status.alive?(connected?'Connected · Ready':'Connecting…'):'Awaiting start';
    $('#send').disabled=!status.alive||sending;$('#interrupt').disabled=status.status!=='working';
    if(!status.alive)notice('Jaime must start this agent before it can receive messages.');
    if(status.surface_ws&&status.surface_ws!==activeWorkspace){activeWorkspace=status.surface_ws;$('#surface').src=me.surface_origin+'/?embed=1&view=presenter&presentation=portal';$('#surface').hidden=false;$('#surfaceEmpty').hidden=true;}
  }
  async function start(){
    me=await api('/api/me');await dependencies();$('#name').textContent=me.name;document.title=me.name;$('#login').hidden=true;$('#workspace').hidden=false;$('#logout').hidden=false;$('#password').value='';notice('');
    transcript=new ConductorHistoryView({element:$('#messages'),render,get:api,error:notice});transcript.select('agent','/api/history');stream();
    const refresh=()=>api('/api/status').then(paintStatus).catch(e=>notice(e.message));await refresh();statusTimer=setInterval(refresh,10000);
  }
  $('#loginForm').onsubmit=async event=>{event.preventDefault();const b=event.submitter;b.disabled=true;$('#loginError').textContent='';try{await api('/api/login',{method:'POST',body:{username:$('#username').value,password:$('#password').value}});await start();}catch(e){$('#loginError').textContent=e.message;}finally{b.disabled=false;}};
  $('#logout').onclick=async()=>{try{await api('/api/logout',{method:'POST'});signedOut();}catch(e){notice(e.message);}};
  $('#composer').onsubmit=async event=>{
    event.preventDefault();const text=$('#message').value;if(!text.trim()||$('#send').disabled)return;
    pending=pending&&pending.text===text?pending:{text,request_id:crypto.randomUUID()};sending=true;$('#send').disabled=true;
    try{await api('/api/send',{method:'POST',body:pending});if($('#message').value===text)$('#message').value='';pending=null;notice('');}
    catch(e){notice(e.message+' Your message is retained; retry uses the same request ID.');}
    finally{sending=false;$('#send').disabled=false;}
  };
  $('#message').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('#composer').requestSubmit();}};
  $('#interrupt').onclick=async()=>{try{await api('/api/interrupt',{method:'POST'});}catch(e){notice(e.message);}};
  $('#closeDetail').onclick=()=>$('#detail').close();
  start().catch(()=>signedOut());
})();
